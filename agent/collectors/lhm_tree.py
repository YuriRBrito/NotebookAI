"""Parser PURO da arvore JSON do LibreHardwareMonitor (http://localhost:8085/data.json).

Etapa 1 da integracao: este modulo NAO faz rede, NAO abre threads e NAO importa nada do resto do
projeto. Recebe o JSON (texto, bytes ou dict) e devolve sensores ja normalizados.

Estrutura real observada (LHM com Remote Web Server, Windows pt-BR):
    raiz -> computador -> hardware (tem "HardwareId") -> grupo ("Voltages", "Clocks"...) -> sensor
    sensor: "SensorId", "Type", "Value"/"Min"/"Max" (texto formatado, ex. "0,813 V", "9,5 MB/s")
    e "RawValue"/"RawMin"/"RawMax" (mesmo formato de texto; em Throughput o raw vem em B/s).

Unidade efetiva do campo bruto (verificada numericamente contra o Value no JSON real):
    o Raw* traz a unidade por escrito em todos os tipos, EXCETO:
      TimeSpan -> numero puro em SEGUNDOS (RawValue "10707" == Value "2:58:27");
      Factor   -> numero puro, adimensional.
    Throughput: Value usa KB/s ou MB/s, o Raw usa B/s. Data/SmallData/Energy/Temperature/Clock: o
    Raw repete a mesma unidade do Value (GB, MB, mWh, °C, MHz).
    Numero SEM unidade em qualquer outro tipo => valor indisponivel (None). Nunca se presume.

Separadores numericos: o LHM formata com a cultura do Windows (pt-BR: decimal ','; o JSON real nao
tem separador de milhar). Para nao confundir '58.003' (58 mil, milhar pt-BR) com 58,003, o parser
trabalha com UM separador decimal por documento (detect_decimal) e valida o agrupamento de milhar.
Sem evidencia no documento, assume pt-BR (decimal ',').

Regras:
- Hardware, categoria e nome vem da propria arvore (HardwareId do ancestral mais proximo).
- Valor ausente ("-", vazio, NaN), unidade ausente/desconhecida ou numero ambiguo => None, com o
  motivo em `note`. Nunca vira zero e nunca se chuta escala.
- Valores saem SEMPRE na unidade canonica do tipo (CANON_UNIT), a mesma de collectors/lhm.py.
"""
import json
import math
import re
from dataclasses import dataclass, field

MAX_DEPTH = 64          # protecao contra JSON malformado/profundo demais

# Unidade canonica por tipo de sensor (igual ao UNITS de collectors/lhm.py; "Timing" e novo).
CANON_UNIT = {
    "Temperature": "°C", "Load": "%", "Clock": "MHz", "Power": "W", "Voltage": "V", "Current": "A",
    "Data": "GB", "SmallData": "MB", "Throughput": "B/s", "Energy": "mWh", "Level": "%",
    "Fan": "RPM", "Frequency": "Hz", "TimeSpan": "s", "Factor": "", "Flow": "L/h", "Control": "%",
    "Noise": "dBA", "Timing": "ns", "Humidity": "%", "Conductivity": "µS/cm",
}
_TYPE_BY_TOKEN = {k.lower(): k for k in CANON_UNIT}      # 'smalldata' -> 'SmallData'

# Unica excecao a "numero sem unidade e indisponivel": tipos em que o LHM entrega numero puro.
_BARE_UNIT = {"TimeSpan": "s", "Factor": ""}

# Binario (LHM usa 1024): confere com o dado real, 9921082 B/s aparece como "9,5 MB/s".
_BIN = {"b": 1.0, "kb": 2.0 ** 10, "kib": 2.0 ** 10, "mb": 2.0 ** 20, "mib": 2.0 ** 20,
        "gb": 2.0 ** 30, "gib": 2.0 ** 30, "tb": 2.0 ** 40, "tib": 2.0 ** 40}

# multiplicador: valor_na_unidade_lida * fator = valor_na_unidade_canonica
_SCALES: dict[str, dict[str, float]] = {
    "Voltage": {"v": 1.0, "mv": 1e-3, "uv": 1e-6},
    "Current": {"a": 1.0, "ma": 1e-3},
    "Power": {"w": 1.0, "mw": 1e-3, "kw": 1e3},
    "Clock": {"mhz": 1.0, "ghz": 1e3, "khz": 1e-3, "hz": 1e-6},
    "Frequency": {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9},
    "Energy": {"mwh": 1.0, "wh": 1e3, "kwh": 1e6},
    "Load": {"%": 1.0}, "Level": {"%": 1.0}, "Control": {"%": 1.0}, "Humidity": {"%": 1.0},
    "Fan": {"rpm": 1.0},
    "Noise": {"dba": 1.0, "db": 1.0},
    "Flow": {"l/h": 1.0},
    "Timing": {"ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9},
    "Conductivity": {"us/cm": 1.0},
    "TimeSpan": {"s": 1.0},
    "Data": {k: v / 2.0 ** 30 for k, v in _BIN.items()},             # canonico: GB
    "SmallData": {k: v / 2.0 ** 20 for k, v in _BIN.items()},        # canonico: MB
    "Throughput": {k + "/s": v for k, v in _BIN.items()},            # canonico: B/s
}

_NULLS = {"", "-", "--", "n/a", "na", "nan", "null", "none"}
_NUM = re.compile(r"^([+-]?\d[\d.,]*)\s*(.*)$")
_VALUE_KEYS = ("Value", "Min", "Max", "RawValue", "RawMin", "RawMax")


class LhmFormatError(ValueError):
    """O conteudo recebido nao e uma arvore do LibreHardwareMonitor."""


class _UnitError(Exception):
    """Unidade (ou tipo) que nao permite converter com seguranca."""


# ----------------------------------------------------------------------
# numeros e unidades
# ----------------------------------------------------------------------
def _clean(text) -> str:
    return str(text).replace("\u00a0", " ").replace("\u2212", "-").strip()


def _norm_unit(unit: str) -> str:
    u = unit.lower().replace("º", "°").replace("µ", "u").replace("μ", "u")
    return re.sub(r"\s+", "", u)


def parse_number(s: str, decimal: str = ",") -> float:
    """Converte numero com UM separador decimal definido ('.' ou ',').
    O outro caractere so e aceito como milhar, em grupos de 3 digitos e sem zero a esquerda:
        decimal=',' : '58.003' -> 58003 | '1.398,5' -> 1398.5 | '0,813' -> 0.813 | '0.813' -> erro
        decimal='.' : '58.003' -> 58.003 | '1,398.5' -> 1398.5 | '1,234' -> 1234 | '0,813' -> erro
    Qualquer outra combinacao (agrupamento invalido, separador repetido, ponto final) levanta
    ValueError: prefere-se 'indisponivel' a um valor errado por mil."""
    if decimal not in (",", "."):
        raise ValueError(f"separador decimal invalido: {decimal!r}")
    thou = "." if decimal == "," else ","
    s = s.strip()
    sign = ""
    if s[:1] in "+-":
        sign, s = s[0], s[1:]
    int_part, sep, frac = s.partition(decimal)
    if thou in frac or decimal in frac:
        raise ValueError(f"numero ambiguo: {s!r}")
    if thou in int_part:
        if not re.fullmatch(r"[1-9]\d{0,2}(?:" + re.escape(thou) + r"\d{3})+", int_part):
            raise ValueError(f"agrupamento de milhar invalido: {s!r}")
        int_part = int_part.replace(thou, "")
    if not int_part.isdigit() or (sep and not frac.isdigit()):
        raise ValueError(f"numero invalido: {s!r}")
    return float(sign + int_part + ("." + frac if sep else ""))


def detect_decimal(texts) -> str:
    """Descobre o separador decimal do documento ('.' ou ',') a partir dos textos de valor.
    So votam os numeros inequivocos: com os dois separadores (vale o ultimo), com separador repetido
    (e milhar; o outro e o decimal), com != 3 digitos depois do separador, ou com zero a esquerda
    ('0,813'). Um numero como '58.003' ou '1,398' e ambiguo e nao vota. Empate/sem votos => ','."""
    votes = {",": 0, ".": 0}
    for t in texts:
        if t is None:
            continue
        m = _NUM.match(_clean(t))
        if not m:
            continue
        num = m.group(1)
        c, d = num.count(","), num.count(".")
        if c and d:
            votes["," if num.rfind(",") > num.rfind(".") else "."] += 1
        elif c or d:
            sep = "," if c else "."
            if (c or d) > 1:
                votes["." if sep == "," else ","] += 1
            else:
                before, after = num.split(sep)
                before = before.lstrip("+-")
                if len(after) != 3 or before.startswith("0") or len(before) > 3:
                    votes[sep] += 1
    return "." if votes["."] > votes[","] else ","


def _timespan(t: str, decimal: str) -> float:
    """'H:MM:SS', 'D:H:MM:SS' ou 'M:SS' -> segundos."""
    parts = t.split(":")
    if not 2 <= len(parts) <= 4:
        raise ValueError(t)
    secs = parse_number(parts[-1], decimal)
    for part, m in zip(reversed(parts[:-1]), (60.0, 3600.0, 86400.0)):
        secs += int(part) * m
    return secs


def _to_canonical(num: float, stype: str, unit: str) -> float:
    """Converte para a unidade canonica do tipo. Levanta _UnitError se nao der para ter certeza."""
    if stype not in CANON_UNIT:
        raise _UnitError("tipo de sensor desconhecido")
    u = _norm_unit(unit)
    if u == "":
        if stype not in _BARE_UNIT:
            raise _UnitError("unidade ausente")
        u = _BARE_UNIT[stype]                         # TimeSpan: segundos; Factor: adimensional
    if stype == "Factor":
        if u == "":
            return num
        raise _UnitError(f"unidade inesperada para Factor ({unit!r})")
    if stype == "Temperature":
        if u in ("°c", "c"):
            return num
        if u in ("°f", "f"):
            return (num - 32.0) * 5.0 / 9.0
        if u in ("k", "°k"):
            return num - 273.15
        raise _UnitError(f"unidade desconhecida ({unit!r})")
    factor = _SCALES[stype].get(u)
    if factor is None:
        raise _UnitError(f"unidade desconhecida para {stype} ({unit!r})")
    return num * factor


def parse_value(text, stype: str, decimal: str = ",") -> tuple[float | None, str]:
    """Texto do LHM -> (valor na unidade canonica | None, motivo se None)."""
    if text is None:
        return None, "sem valor"
    t = _clean(text)
    if t.lower() in _NULLS:
        return None, "sem valor"
    try:
        if stype == "TimeSpan" and ":" in t:
            val = _timespan(t, decimal)
        else:
            m = _NUM.match(t)
            if not m:
                return None, f"formato nao reconhecido: {t!r}"
            val = _to_canonical(parse_number(m.group(1), decimal), stype, m.group(2))
    except _UnitError as e:
        return None, f"{e}: {t!r}"
    except (ValueError, OverflowError):
        return None, f"formato nao reconhecido ou ambiguo: {t!r}"
    if not math.isfinite(val):
        return None, f"valor nao finito: {t!r}"
    return val, ""


# ----------------------------------------------------------------------
# hardware / categoria
# ----------------------------------------------------------------------
def hardware_type(hardware_id: str) -> str:
    """'/intelcpu/0' -> 'intelcpu'; '/memory/dimm/2' -> 'memory'."""
    parts = [p for p in hardware_id.split("/") if p]
    return parts[0].lower() if parts else "?"


def categorize_hardware(hw_type: str) -> str:
    """Categoria do NotebookAI a partir do tipo de hardware do LHM (inclui 'gpu-intel-integrated')."""
    t = hw_type.lower()
    if t.endswith("cpu"):
        return "cpu"
    if t.startswith("gpu"):
        return "gpu"
    if t in ("nvme", "hdd", "ssd", "storage"):
        return "ssd"
    if t in ("ram", "vram", "memory"):
        return "ram"
    if t == "battery":
        return "battery"
    if t == "nic":
        return "network"
    if t in ("motherboard", "lpc", "superio", "embeddedcontroller"):
        return "motherboard"
    return "other"


def split_sensor_id(sensor_id: str) -> tuple[str, str, str] | None:
    """'/intelcpu/0/voltage/0' -> ('/intelcpu/0', 'voltage', '0'). None se curto demais."""
    parts = [p for p in sensor_id.split("/") if p]
    if len(parts) < 3:
        return None
    return "/" + "/".join(parts[:-2]), parts[-2], parts[-1]


# ----------------------------------------------------------------------
# resultado
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class LhmSensor:
    sensor_id: str
    hardware_id: str
    hardware_name: str
    hardware_type: str       # 'intelcpu', 'nvme', 'gpu-intel-integrated', 'battery', 'ram', 'vram'...
    category: str            # cpu | gpu | ssd | ram | battery | network | motherboard | other
    group: str               # rotulo do grupo na arvore ("Temperatures"); so informativo
    name: str
    type: str                # 'Temperature', 'Load', 'Throughput'...
    unit: str                # unidade canonica do tipo
    value: float | None      # None = indisponivel (nunca 0 no lugar)
    vmin: float | None
    vmax: float | None
    note: str = ""           # motivo quando value e None
    text: str = ""           # "Value" original, so para diagnostico


@dataclass
class LhmTree:
    sensors: list[LhmSensor] = field(default_factory=list)
    hardware: dict[str, dict] = field(default_factory=dict)    # id -> {"name","type","category"}
    warnings: list[str] = field(default_factory=list)
    decimal: str = ","                                         # separador decimal detectado


def decode_payload(raw) -> dict:
    """bytes/str/dict -> dict. Aceita BOM UTF-8 (arquivos salvos pelo PowerShell o trazem)."""
    if isinstance(raw, dict):
        return raw
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8-sig")
        data = json.loads(str(raw).lstrip("\ufeff"))
    except (ValueError, UnicodeDecodeError) as e:
        raise LhmFormatError(f"JSON invalido: {e}") from e
    return data


def _pick(node: dict, raw_key: str, fmt_key: str, stype: str, decimal: str) -> tuple[float | None, str]:
    """Prefere o campo Raw* (mais preciso); cai para o formatado se o Raw nao servir."""
    why = "sem valor"
    for k in (raw_key, fmt_key):
        if k in node:
            val, w = parse_value(node[k], stype, decimal)
            if val is not None:
                return val, ""
            why = w
    return None, why


def _build_sensor(node: dict, hw: tuple[str, str] | None, group: str, tree: LhmTree) -> None:
    sid = str(node.get("SensorId") or "")
    parts = split_sensor_id(sid)
    stype = str(node.get("Type") or "")
    if not stype and parts:
        stype = _TYPE_BY_TOKEN.get(parts[1].lower(), "")
    if not sid or not stype:
        tree.warnings.append(f"sensor sem SensorId/Type ignorado: {node.get('Text')!r}")
        return
    if hw is not None:
        hw_id, hw_name = hw
    elif parts:
        hw_id, hw_name = parts[0], ""
    else:
        hw_id, hw_name = "", ""
    htype = hardware_type(hw_id)
    dec = tree.decimal
    value, why = _pick(node, "RawValue", "Value", stype, dec)
    vmin, _ = _pick(node, "RawMin", "Min", stype, dec)
    vmax, _ = _pick(node, "RawMax", "Max", stype, dec)
    tree.sensors.append(LhmSensor(
        sensor_id=sid, hardware_id=hw_id, hardware_name=hw_name, hardware_type=htype,
        category=categorize_hardware(htype), group=group, name=str(node.get("Text") or ""),
        type=stype, unit=CANON_UNIT.get(stype, ""), value=value, vmin=vmin, vmax=vmax,
        note="" if value is not None else why, text=str(node.get("Value") or "")))


def _walk(node, depth: int, hw: tuple[str, str] | None, group: str, tree: LhmTree) -> None:
    if not isinstance(node, dict):
        return
    if depth > MAX_DEPTH:
        msg = f"arvore mais funda que {MAX_DEPTH} niveis; ramo ignorado"
        if msg not in tree.warnings:
            tree.warnings.append(msg)
        return
    if "HardwareId" in node:                                   # hardware (pode ser aninhado)
        hw_id = str(node["HardwareId"])
        hw = (hw_id, str(node.get("Text") or ""))
        htype = hardware_type(hw_id)
        tree.hardware[hw_id] = {"name": hw[1], "type": htype, "category": categorize_hardware(htype)}
        group = ""
    elif "SensorId" in node:                                   # folha: sensor
        _build_sensor(node, hw, group, tree)
        return
    else:                                                      # raiz, computador ou grupo
        group = str(node.get("Text") or "")
    for child in node.get("Children") or []:
        _walk(child, depth + 1, hw, group, tree)


def _numeric_texts(root) -> list:
    """Todos os textos de valor dos sensores (para detectar o separador decimal do documento)."""
    out, stack = [], [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict) or depth > MAX_DEPTH:
            continue
        if "HardwareId" not in node and "SensorId" in node:
            out.extend(node.get(k) for k in _VALUE_KEYS)
            continue
        stack.extend((c, depth + 1) for c in node.get("Children") or [])
    return out


def parse_tree(tree_or_raw) -> LhmTree:
    """Ponto de entrada: dict, str ou bytes do data.json -> LhmTree (sensores + hardware + avisos)."""
    root = decode_payload(tree_or_raw)
    if not isinstance(root, dict) or not isinstance(root.get("Children"), list):
        raise LhmFormatError("JSON sem 'Children': nao parece a arvore do LibreHardwareMonitor")
    out = LhmTree(decimal=detect_decimal(_numeric_texts(root)))
    _walk(root, 0, None, "", out)
    return out


# ----------------------------------------------------------------------
# utilitarios
# ----------------------------------------------------------------------
def select(sensors, *, category=None, hw_type=None, stype=None, name=None) -> list[LhmSensor]:
    """Filtra por categoria, tipo de hardware, tipo de sensor e nome (texto exato sem diferenciar
    maiusculas, ou regex compilada). Identificacao por significado, nunca por ID fixo."""
    out = []
    for s in sensors:
        if category and s.category != category:
            continue
        if hw_type and s.hardware_type != hw_type.lower():
            continue
        if stype and s.type != stype:
            continue
        if name is not None:
            ok = name.search(s.name) if hasattr(name, "search") else s.name.lower() == str(name).lower()
            if not ok:
                continue
        out.append(s)
    return out


def to_rows(sensors) -> list[dict]:
    """Converte para o formato de linha (classe WMI Sensor) que collectors/lhm.py::build_readings ja
    entende. Parent = id do hardware, como no WMI. Permite plugar este parser sem mexer no resto."""
    return [{"Identifier": s.sensor_id, "Name": s.name, "SensorType": s.type, "Value": s.value,
             "Min": s.vmin, "Max": s.vmax, "Parent": s.hardware_id} for s in sensors]
# FIM lhm_tree.py