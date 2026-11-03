"""Testes do parser puro lhm_tree.py (etapa 1). Nao usa rede nem Windows.

Casos sinteticos sempre rodam. O teste com o data.json REAL do notebook roda se existir o arquivo
tests/data/lhm_data_sample.json (copie o seu data_sample.json para la); senao e apenas pulado.
O arquivo termina com a linha '# FIM test_lhm_tree.py' (confere se a copia veio completa).
"""
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.collectors import lhm_tree as T

MB = 2 ** 20


def close(a, b, tol=1e-9):
    return a is not None and abs(a - b) <= tol * max(1.0, abs(b))


def raises_value_error(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return True
    return False


# ----------------------------------------------------------------------
# numeros e separadores
# ----------------------------------------------------------------------
def test_parse_number_ptbr():
    N = T.parse_number                                        # padrao: decimal ','
    assert N("0,813") == 0.813
    assert N("1397,8") == 1397.8
    assert N("13,750") == 13.75
    assert N("58003") == 58003.0
    assert N("-3,5") == -3.5
    assert N("58.003") == 58003.0                             # milhar pt-BR, NAO 58,003
    assert N("1.398") == 1398.0
    assert N("1.398,5") == 1398.5
    assert N("3.324,0") == 3324.0
    assert N("1.234.567") == 1234567.0
    # invalidos em pt-BR: erro, nunca um valor chutado
    for bad in ("0.813", "1.39", "12.5", "1,2,3", "1.2.3", "1,398.5", "12,", "01.234", "1.2345", "1..234"):
        assert raises_value_error(N, bad), bad
    print("OK test_parse_number_ptbr")


def test_parse_number_enus():
    N = lambda s: T.parse_number(s, ".")
    assert N("0.625") == 0.625
    assert N("12.5") == 12.5
    assert N("1,398.5") == 1398.5
    assert N("1,234") == 1234.0
    assert N("1,234,567") == 1234567.0
    for bad in ("0,813", "1.398,5", "12,5", "1.2.3"):
        assert raises_value_error(N, bad), bad
    assert raises_value_error(T.parse_number, "1", ";")
    print("OK test_parse_number_enus")


def test_detect_decimal():
    D = T.detect_decimal
    assert D(["0,813 V", "1397,8 MHz", "58003 mWh"]) == ","
    assert D(["0.625 ns", "13.750 ns", "12.5 W"]) == "."
    assert D(["58003 mWh", "145", None, "-"]) == ","                   # sem evidencia: pt-BR
    assert D(["13.750 ns", "1.398 MHz"]) == ","                       # so ambiguos: pt-BR
    assert D(["58.003 mWh", "3,5 W"]) == ","
    assert D(["58.003 mWh", "3.5 W"]) == "."
    assert D(["1.234,5 W", "2.000,0 W"]) == ","                       # ambos separadores
    assert D(["1,234.5 W"]) == "."
    assert D(["1.234.567 MB"]) == ","                                 # ponto repetido = milhar
    assert D(["0,813 V"]) == ","                                      # zero a esquerda = decimal
    assert D(["0.813 V"]) == "."
    assert D(["2:58:27", "10707"]) == ","                             # tempo nao vota
    print("OK test_detect_decimal")


def test_thousands_fallback_text():
    """Fallback textual (sem Raw*): '58.003 mWh' em pt-BR sao 58 mil mWh, nao 58,003."""
    P = T.parse_value
    assert P("58.003 mWh", "Energy")[0] == 58003.0
    assert P("58003 mWh", "Energy")[0] == 58003.0
    assert P("1.398 MHz", "Clock")[0] == 1398.0
    assert P("3.324,0 GB", "Data")[0] == 3324.0
    assert P("1.234,5 W", "Power")[0] == 1234.5
    assert P("0,813 V", "Voltage")[0] == 0.813
    assert P("58.003 mWh", "Energy")[0] != 58.003
    # mesma string, cultura en-US explicita
    assert P("58.003 mWh", "Energy", ".")[0] == 58.003
    assert P("1,398 MHz", "Clock", ".")[0] == 1398.0
    # ponto decimal em documento pt-BR e ambiguo => indisponivel, nunca 0,813 chutado
    v, why = P("0.813 V", "Voltage")
    assert v is None and "ambiguo" in why, (v, why)

    def energy_tree(energy_text, power_text, with_raw):
        e = _sensor(1, "Remaining Capacity", "Energy", energy_text, sid="/battery/B/energy/2",
                    raw=energy_text if with_raw else None)
        p = _sensor(2, "Discharge Rate", "Power", power_text, sid="/battery/B/power/0")
        return {"Children": [_hw("/battery/B", "Bat", [_group("G", [e, p])])]}

    for with_raw in (False, True):
        tr = T.parse_tree(energy_tree("58.003 mWh", "3,5 W", with_raw))          # documento pt-BR
        assert tr.decimal == ","
        s = {x.type: x for x in tr.sensors}
        assert s["Energy"].value == 58003.0 and s["Energy"].vmin == 58003.0 and s["Power"].value == 3.5
        tr = T.parse_tree(energy_tree("58.003 mWh", "3.5 W", with_raw))          # documento en-US
        assert tr.decimal == "."
        s = {x.type: x for x in tr.sensors}
        assert s["Energy"].value == 58.003 and s["Power"].value == 3.5
    tr = T.parse_tree(energy_tree("58.003 mWh", "-", False))                    # so ambiguo: pt-BR
    assert [x.value for x in tr.sensors if x.type == "Energy"] == [58003.0]
    print("OK test_thousands_fallback_text")


# ----------------------------------------------------------------------
# unidades
# ----------------------------------------------------------------------
def test_parse_value_units():
    P = T.parse_value
    assert close(P("0,813 V", "Voltage")[0], 0.813)
    assert close(P("100 mV", "Voltage")[0], 0.1)
    assert close(P("14500 mW", "Power")[0], 14.5)
    assert close(P("6,3 W", "Power")[0], 6.3)
    assert close(P("3,2 GHz", "Clock")[0], 3200.0)
    assert close(P("1397,8 MHz", "Clock")[0], 1397.8)
    assert close(P("47 \u00b0C", "Temperature")[0], 47.0)
    assert close(P("47\u00a0\u00b0C", "Temperature")[0], 47.0)          # NBSP antes da unidade
    assert close(P("116,6 \u00b0F", "Temperature")[0], 47.0)
    assert close(P("58,003 Wh", "Energy")[0], 58003.0)
    assert close(P("58003 mWh", "Energy")[0], 58003.0)
    assert close(P("0,827 A", "Current")[0], 0.827)
    assert close(P("94,7 %", "Level")[0], 94.7)
    assert close(P("0,625 ns", "Timing")[0], 0.625)
    # escalas binarias (LHM usa 1024) e unidades canonicas por tipo
    assert close(P("9,5 MB/s", "Throughput")[0], 9.5 * MB)
    assert close(P("9921082,0 B/s", "Throughput")[0], 9921082.0)
    assert close(P("512 KB/s", "Throughput")[0], 512 * 1024)
    assert close(P("774,2 MB", "SmallData")[0], 774.2)
    assert close(P("1,5 GB", "SmallData")[0], 1536.0)
    assert close(P("1024 MB", "Data")[0], 1.0)
    assert close(P("14,0 GB", "Data")[0], 14.0)
    assert close(P("2 TB", "Data")[0], 2048.0)
    # tempo: formatado (H:MM:SS, D:H:MM:SS, M:SS) e bruto (segundos puros)
    assert P("2:58:27", "TimeSpan")[0] == 10707.0
    assert P("3:0:22:51", "TimeSpan")[0] == 3 * 86400 + 22 * 60 + 51
    assert P("12:30", "TimeSpan")[0] == 750.0
    assert P("10707", "TimeSpan")[0] == 10707.0
    assert P("145,000", "Factor")[0] == 145.0
    print("OK test_parse_value_units")


def test_null_is_not_zero():
    P = T.parse_value
    for txt in (None, "", "-", "  ", "N/A", "NaN"):
        v, why = P(txt, "Load")
        assert v is None and why, (txt, v, why)
    v, why = P("0,0 %", "Load")
    assert v == 0.0 and v is not None and why == ""                       # zero REAL continua zero
    assert P("abc", "Load")[0] is None
    print("OK test_null_is_not_zero")


def test_units_missing_or_unknown():
    """Sem unidade confiavel => None com motivo. Nunca um numero 'valido' inventado."""
    P = T.parse_value
    # numero sem unidade: so TimeSpan (segundos) e Factor (adimensional) sao aceitos
    for txt, st in (("47", "Temperature"), ("14,0", "Data"), ("774,2", "SmallData"), ("1397,8", "Clock"),
                    ("58003", "Energy"), ("9921082,0", "Throughput"), ("0,5", "Voltage"), ("8,8", "Load"),
                    ("6,3", "Power"), ("0,8", "Current"), ("94,7", "Level")):
        v, why = P(txt, st)
        assert v is None and "unidade ausente" in why, (txt, st, v, why)
    assert P("10707", "TimeSpan") == (10707.0, "")
    assert P("145,000", "Factor") == (145.0, "")
    # unidade desconhecida ou de outra grandeza
    for txt, st in (("12 furlongs", "Power"), ("5 GB/s", "Data"), ("5 W", "Voltage"), ("100 MB", "Throughput"),
                    ("100 B/s", "Data"), ("5 mW", "Clock"), ("3 \u00b0C", "Load"), ("47 V", "Temperature"),
                    ("3 MB", "Energy"), ("5 %", "Power"), ("12 x", "Factor"), ("3 kg", "Level")):
        v, why = P(txt, st)
        assert v is None and "unidade" in why, (txt, st, v, why)
    # tipo desconhecido: nem com unidade, nem sem
    for txt in ("3,5", "3,5 W", "3,5 MB"):
        v, why = P(txt, "Quark")
        assert v is None and "tipo de sensor desconhecido" in why, (txt, v, why)
    assert P("1:2:3:4:5", "TimeSpan")[0] is None
    assert P("1:xx", "TimeSpan")[0] is None

    # na arvore: valor, min e max ficam None (nunca 0), com motivo; sensores vizinhos nao sao afetados
    nodes = [
        _sensor(1, "Sem unidade", "Temperature", "47", sid="/intelcpu/0/temperature/1"),
        _sensor(2, "Unidade desconhecida", "Power", "12 furlongs", sid="/intelcpu/0/power/1"),
        _sensor(3, "Tipo desconhecido", "Quark", "3,5 W", sid="/intelcpu/0/quark/1"),
        _sensor(4, "Normal", "Power", "8,5 W", sid="/intelcpu/0/power/2"),
        # Raw ilegivel, formatado bom: usa o formatado; ambos ruins: indisponivel
        _sensor(5, "Raw ruim", "Power", "5,0 W", raw="5 furlongs", sid="/intelcpu/0/power/3"),
        _sensor(6, "Ambos ruins", "Power", "5 furlongs", raw="5 furlongs", sid="/intelcpu/0/power/4"),
    ]
    tr = T.parse_tree({"Children": [_hw("/intelcpu/0", "CPU", [_group("G", nodes)])]})
    by = {s.name: s for s in tr.sensors}
    for n in ("Sem unidade", "Unidade desconhecida", "Tipo desconhecido", "Ambos ruins"):
        s = by[n]
        assert s.value is None and s.vmin is None and s.vmax is None and s.note, s
    assert by["Sem unidade"].note.startswith("unidade ausente")
    assert by["Normal"].value == 8.5
    assert by["Raw ruim"].value == 5.0 and by["Raw ruim"].note == ""
    print("OK test_units_missing_or_unknown")


# ----------------------------------------------------------------------
# arvore sintetica
# ----------------------------------------------------------------------
def _sensor(i, text, stype, val, vmin=None, vmax=None, raw=None, sid=None):
    n = {"id": i, "Text": text, "Min": vmin if vmin is not None else val, "Value": val,
         "Max": vmax if vmax is not None else val, "SensorId": sid, "Type": stype,
         "ImageURL": "images/transparent.png", "Children": []}
    if raw is not None:
        n.update({"RawValue": raw, "RawMin": raw, "RawMax": raw})
    return n


def _group(text, children):
    return {"id": 0, "Text": text, "Min": "", "Value": "", "Max": "", "ImageURL": "", "Children": children}


def _hw(hid, text, children):
    d = _group(text, children)
    d["HardwareId"] = hid
    return d


def synthetic_tree():
    cpu = _hw("/intelcpu/0", "Intel Core i5", [
        _group("Temperatures", [_sensor(1, "CPU Package", "Temperature", "50,0 \u00b0C", sid="/intelcpu/0/temperature/12"),
                                _sensor(2, "Sem Valor", "Temperature", "-", sid="/intelcpu/0/temperature/99")]),
        _group("Powers", [_sensor(3, "CPU Package", "Power", "8,5 W", sid="/intelcpu/0/power/0")]),
    ])
    gpu = _hw("/gpu-intel-integrated/%5C%5C%3FPCI", "Intel UHD", [
        _group("Data", [_sensor(4, "D3D Shared Memory Used", "SmallData", "772,0 MB", sid="/gpu-intel-integrated/%5C%5C%3FPCI/smalldata/0")]),
    ])
    nvme = _hw("/nvme/0", "SSD X", [
        _group("Throughput", [_sensor(5, "Write Rate", "Throughput", "9,5 MB/s", raw="9921082,0 B/s", sid="/nvme/0/throughput/55")]),
        _group("Data", [_sensor(6, "Free Space", "Data", "168,0 GB", sid="/nvme/0/data/31")]),
    ])
    sub = _hw("/lpc/it8688e", "SuperIO", [_group("Fans", [_sensor(7, "Fan #1", "Fan", "2100 RPM", sid="/lpc/it8688e/fan/0")])])
    board = _hw("/motherboard", "Placa", [sub])                           # hardware aninhado
    bat = _hw("/battery/DELL-1", "Bateria", [
        _group("Capacities", [_sensor(8, "Fully-Charged Capacity", "Energy", "40310 mWh", sid="/battery/DELL-1/energy/1")]),
        _group("Times", [_sensor(9, "Remaining Time (Estimated)", "TimeSpan", "2:58:27", raw="10707", sid="/battery/DELL-1/timespan/0")]),
    ])
    comp = _group("MEUPC", [board, cpu, gpu, nvme, bat])
    comp["ImageURL"] = "images_icon/computer.png"
    return {"id": 0, "Text": "Sensor", "Min": "Min", "Value": "Value", "Max": "Max", "ImageURL": "", "Children": [comp]}


def test_tree_synthetic():
    tr = T.parse_tree(synthetic_tree())
    by = {s.sensor_id: s for s in tr.sensors}
    assert len(tr.sensors) == 9 and not tr.warnings and tr.decimal == ","
    pk = by["/intelcpu/0/temperature/12"]
    assert (pk.category, pk.hardware_type, pk.hardware_name, pk.group, pk.type, pk.unit) == \
           ("cpu", "intelcpu", "Intel Core i5", "Temperatures", "Temperature", "\u00b0C")
    assert pk.value == 50.0
    nv = by["/intelcpu/0/temperature/99"]
    assert nv.value is None and nv.note == "sem valor"                      # nulo != zero
    g = by["/gpu-intel-integrated/%5C%5C%3FPCI/smalldata/0"]
    assert g.category == "gpu" and g.hardware_type == "gpu-intel-integrated" and g.value == 772.0 and g.unit == "MB"
    w = by["/nvme/0/throughput/55"]
    assert w.category == "ssd" and w.value == 9921082.0 and w.unit == "B/s"   # usa Raw (B/s), nao "9,5 MB/s"
    assert by["/nvme/0/data/31"].value == 168.0
    f = by["/lpc/it8688e/fan/0"]
    assert f.hardware_id == "/lpc/it8688e" and f.category == "motherboard" and f.value == 2100.0   # aninhado
    assert by["/battery/DELL-1/timespan/0"].value == 10707.0
    assert set(tr.hardware) == {"/motherboard", "/lpc/it8688e", "/intelcpu/0", "/gpu-intel-integrated/%5C%5C%3FPCI",
                                "/nvme/0", "/battery/DELL-1"}
    # selecao por significado
    assert [s.value for s in T.select(tr.sensors, category="cpu", stype="Power", name="cpu package")] == [8.5]
    assert len(T.select(tr.sensors, name=re.compile(r"^Fully", re.I))) == 1
    assert len(T.select(tr.sensors, hw_type="nvme")) == 2
    print("OK test_tree_synthetic")


def test_robustness():
    # BOM em bytes e em texto
    raw = json.dumps(synthetic_tree()).encode("utf-8")
    assert len(T.parse_tree(b"\xef\xbb\xbf" + raw).sensors) == 9
    assert len(T.parse_tree("\ufeff" + raw.decode()).sensors) == 9
    # lixo
    for bad in ("nao e json", "[]", "{}", b"\xff\xfe\x00"):
        try:
            T.parse_tree(bad)
        except T.LhmFormatError:
            pass
        else:
            raise AssertionError(f"deveria falhar: {bad!r}")
    # arvore vazia mas valida: zero sensores, sem excecao
    assert T.parse_tree({"Children": []}).sensors == []
    # tipo ausente: deduz do SensorId
    node = _sensor(1, "X", None, "0,5 V", sid="/intelcpu/0/voltage/0")
    del node["Type"]
    tr = T.parse_tree({"Children": [_hw("/intelcpu/0", "CPU", [node])]})
    assert tr.sensors[0].type == "Voltage" and tr.sensors[0].value == 0.5
    # sensor sem id: aviso, sem excecao
    tr = T.parse_tree({"Children": [_hw("/x/0", "X", [{"Text": "?", "SensorId": "", "Children": []}])]})
    assert tr.sensors == [] and tr.warnings
    # profundidade absurda: aviso, sem RecursionError
    deep = {"Text": "leaf", "Children": []}
    for _ in range(500):
        deep = {"Text": "g", "Children": [deep]}
    tr = T.parse_tree({"Children": [deep]})
    assert tr.warnings and "funda" in tr.warnings[0]
    # valores nao-string (null do JSON) nao quebram
    n = _sensor(1, "N", "Load", None, sid="/intelcpu/0/load/0")
    tr = T.parse_tree({"Children": [_hw("/intelcpu/0", "CPU", [n])]})
    assert tr.sensors[0].value is None and tr.sensors[0].note == "sem valor"
    print("OK test_robustness")


def test_to_rows_feeds_existing_build_readings():
    """Os dados do parser alimentam o build_readings ATUAL sem alteracao (prova de compatibilidade)."""
    from agent.collectors.lhm import build_readings
    rows = T.to_rows(T.parse_tree(synthetic_tree()).sensors)
    k = {r.key: r for r in build_readings(rows, "LibreHardwareMonitor (HTTP)")}
    assert k["cpu.package_temp_c"].value == 50.0
    assert k["cpu.package_power_w"].value == 8.5
    unmapped_null = [r for r in k.values() if r.sensor_id == "/intelcpu/0/temperature/99"][0]
    assert unmapped_null.value is None and "null" in unmapped_null.note
    print("OK test_to_rows_feeds_existing_build_readings")


# ----------------------------------------------------------------------
# JSON real: verificacao exaustiva contra um oraculo independente
# ----------------------------------------------------------------------
_F = {"V": 1.0, "W": 1.0, "MHz": 1.0, "\u00b0C": 1.0, "%": 1.0, "GB": 1.0, "MB": 1.0, "ns": 1.0, "A": 1.0,
      "mWh": 1.0, "B/s": 1.0, "KB/s": 1024.0, "MB/s": 1048576.0}
_ORACLE_RE = re.compile(r"^(-?\d+(?:,\d+)?)(?: (.+))?$")

# unidade efetiva do campo bruto (RawValue/RawMin/RawMax) por tipo, como medida neste JSON
EXPECTED_RAW_UNITS = {
    "Clock": {"MHz"}, "Current": {"A"}, "Data": {"GB"}, "Energy": {"mWh"}, "Level": {"%"},
    "Load": {"%"}, "Power": {"W"}, "SmallData": {"MB"}, "Temperature": {"\u00b0C"},
    "Throughput": {"B/s"}, "Timing": {"ns"}, "Voltage": {"V"},
    "TimeSpan": {""},        # numero puro = SEGUNDOS
    "Factor": {""},          # numero puro, adimensional
}
EXPECTED_TYPE_COUNTS = {"Clock": 12, "Current": 1, "Data": 19, "Energy": 3, "Factor": 2, "Level": 6, "Load": 38,
                        "Power": 6, "SmallData": 3, "Temperature": 27, "Throughput": 12, "TimeSpan": 1,
                        "Timing": 17, "Voltage": 13}
EXPECTED_CATEGORY_COUNTS = {"battery": 9, "cpu": 63, "gpu": 19, "network": 25, "ram": 24, "ssd": 20}
EXPECTED_HARDWARE = {   # nome -> (tipo, categoria, qtde de sensores)
    "Dell 0YHY9P": ("motherboard", "motherboard", 0),
    "12th Gen Intel Core i5-1235U": ("intelcpu", "cpu", 63),
    "Virtual Memory": ("vram", "ram", 3),
    "Total Memory": ("ram", "ram", 3),
    "Samsung - M471A2K43EB1-CWE (#2)": ("memory", "ram", 18),
    "Intel(R) UHD Graphics": ("gpu-intel-integrated", "gpu", 19),
    "UP GAMER FAST PLUS 256GB": ("nvme", "ssd", 20),
    "Bluetooth Network Connection 3": ("nic", "network", 5),
    "Ethernet 4": ("nic", "network", 5),
    "Local Area Connection* 13": ("nic", "network", 5),
    "Local Area Connection* 14": ("nic", "network", 5),
    "Wi-Fi 3": ("nic", "network", 5),
    "DELL H4PVC26": ("battery", "battery", 9),
}
# (sensor_id, nome, tipo, valor, min, max, unidade canonica) com a leitura exata do arquivo de amostra
EXPECTED_VALUES = [
    ("/battery/DELL-H4PVC26_1/energy/0", "Designed Capacity", "Energy", 58003.0, 58003.0, 58003.0, "mWh"),
    ("/battery/DELL-H4PVC26_1/energy/1", "Fully-Charged Capacity", "Energy", 40310.0, 40310.0, 40310.0, "mWh"),
    ("/battery/DELL-H4PVC26_1/energy/2", "Remaining Capacity", "Energy", 38167.0, 38167.0, 39626.0, "mWh"),
    ("/battery/DELL-H4PVC26_1/timespan/0", "Remaining Time (Estimated)", "TimeSpan", 10707.0, 2649.0, 260571.0, "s"),
    ("/battery/DELL-H4PVC26_1/current/0", "Discharge Current", "Current", 0.827, 0.034, 3.621, "A"),
    ("/battery/DELL-H4PVC26_1/voltage/0", "Voltage", "Voltage", 15.516, 14.818, 16.217, "V"),
    ("/battery/DELL-H4PVC26_1/level/1", "Degradation Level", "Level", 30.5, 30.5, 30.5, "%"),
    ("/nvme/0/data/31", "Free Space", "Data", 168.0, 168.0, 168.6, "GB"),
    ("/nvme/0/data/32", "Total Space", "Data", 256.1, 256.1, 256.1, "GB"),
    ("/nvme/0/throughput/54", "Read Rate", "Throughput", 0.0, 0.0, 63195050.0, "B/s"),
    ("/nvme/0/throughput/55", "Write Rate", "Throughput", 9921082.0, 41693.2, 144563500.0, "B/s"),
    ("/nvme/0/level/20", "Life", "Level", 99.0, 0.0, 99.0, "%"),
    ("/nvme/0/factor/24", "Power On Hours", "Factor", 631.0, 0.0, 631.0, ""),
    ("/intelcpu/0/temperature/12", "CPU Package", "Temperature", 44.0, 36.0, 69.0, "\u00b0C"),
    ("/intelcpu/0/power/0", "CPU Package", "Power", 6.3, 1.8, 39.1, "W"),
    ("/intelcpu/0/voltage/0", "CPU Core", "Voltage", 0.813, 0.642, 1.216, "V"),
    ("/intelcpu/0/clock/1", "P-Core #1", "Clock", 1397.8, 698.9, 4393.0, "MHz"),
    ("/intelcpu/0/load/0", "CPU Total", "Load", 8.8, 2.1, 91.7, "%"),
    ("/ram/data/0", "Memory Used", "Data", 11.6, 11.2, 12.3, "GB"),
    ("/ram/data/1", "Memory Available", "Data", 4.1, 3.4, 4.5, "GB"),
]


def _oracle(text, stype):
    """Leitura independente de um campo do sample (so valida o formato deste JSON real).
    -> (valor na unidade canonica, unidade escrita no campo, casas decimais mostradas)."""
    if stype == "TimeSpan" and ":" in text:
        p = [int(x) for x in text.split(":")]
        return float(sum(v * m for v, m in zip(reversed(p), (1, 60, 3600, 86400)))), ":", 0
    m = _ORACLE_RE.match(text)
    assert m, f"formato inesperado no sample: {text!r}"
    num, unit = m.group(1), m.group(2) or ""
    dec = len(num.split(",")[1]) if "," in num else 0
    val = float(num.replace(",", "."))
    if stype in ("TimeSpan", "Factor"):
        assert unit == "", (text, stype)
        return val, "", dec
    return val * _F[unit], unit, dec


def test_real_sample():
    p = Path(__file__).resolve().parent / "data" / "lhm_data_sample.json"
    if not p.exists():
        print("SKIP test_real_sample (copie o data_sample.json para tests/data/lhm_data_sample.json)")
        return
    payload = p.read_bytes()
    raw = json.loads(payload.decode("utf-8-sig"))

    # --- varredura independente do JSON (hardware ancestral, tipo, campos brutos) ---
    nodes = {}

    def walk(n, hw):
        if "HardwareId" in n:
            hw = (n["HardwareId"], n["Text"])
        if "SensorId" in n:
            nodes[n["SensorId"]] = (n, hw)
        for c in n.get("Children", []):
            walk(c, hw)
    walk(raw, None)

    tr = T.parse_tree(payload)
    by = {s.sensor_id: s for s in tr.sensors}

    # --- quantidade, avisos, separador ---
    assert not tr.warnings, tr.warnings
    assert tr.decimal == ","
    assert len(tr.sensors) == 160 and len(by) == 160 and len(nodes) == 160
    assert set(by) == set(nodes), "sensores perdidos ou inventados"
    assert len(tr.hardware) == 13

    # --- tipos, categorias e hardware ---
    counts = {}
    for s in tr.sensors:
        counts[s.type] = counts.get(s.type, 0) + 1
    assert counts == EXPECTED_TYPE_COUNTS, counts
    cats = {}
    for s in tr.sensors:
        cats[s.category] = cats.get(s.category, 0) + 1
    assert cats == EXPECTED_CATEGORY_COUNTS, cats
    got_hw = {h["name"]: (h["type"], h["category"], sum(1 for s in tr.sensors if s.hardware_id == hid))
              for hid, h in tr.hardware.items()}
    assert got_hw == EXPECTED_HARDWARE, got_hw

    # --- cada sensor contra o oraculo: hardware, tipo, unidade, valor, min e max ---
    raw_units = {}
    for sid, (n, hw) in nodes.items():
        s = by[sid]
        assert s.hardware_id == hw[0] and s.hardware_name == hw[1], sid
        assert s.type == n["Type"] and s.name == n["Text"], sid
        assert s.unit == T.CANON_UNIT[s.type], sid
        for fmt_k, raw_k, got in (("Value", "RawValue", s.value), ("Min", "RawMin", s.vmin),
                                  ("Max", "RawMax", s.vmax)):
            assert got is not None and math.isfinite(got), (sid, fmt_k)            # nada virou N/D
            want_raw, u_raw, _ = _oracle(n[raw_k], s.type)
            raw_units.setdefault(s.type, set()).add(u_raw)
            assert close(got, want_raw, 1e-12), (sid, raw_k, got, want_raw)         # valor bruto exato
            want_fmt, u_fmt, dec = _oracle(n[fmt_k], s.type)
            tol = 1e-9 if u_fmt == ":" else 0.5 * 10 ** -dec * _F.get(u_fmt, 1.0) * (1 + 1e-9) + 1e-9
            assert abs(got - want_fmt) <= tol, (sid, fmt_k, got, want_fmt, tol)     # bate com o formatado
    # unidade efetiva do campo bruto, por tipo
    assert raw_units == {k: v for k, v in EXPECTED_RAW_UNITS.items()}, raw_units

    # --- Raw x Value formatado nos tipos pedidos ---
    for sid, (n, hw) in nodes.items():
        t = n["Type"]
        if t in ("Data", "SmallData", "Energy", "Temperature", "Clock"):
            for fk, rk in (("Value", "RawValue"), ("Min", "RawMin"), ("Max", "RawMax")):
                assert n[fk] == n[rk], (sid, fk, n[fk], n[rk])      # raw repete texto e unidade do Value
        if t == "TimeSpan":
            for fk, rk in (("Value", "RawValue"), ("Min", "RawMin"), ("Max", "RawMax")):
                assert ":" in n[fk] and ":" not in n[rk] and "," not in n[rk]      # raw = segundos inteiros
                assert _oracle(n[fk], t)[0] == float(n[rk]), (sid, fk)
        if t == "Throughput":
            assert n["RawValue"].endswith(" B/s") and not n["Value"].endswith(" B/s")

    # --- leituras conhecidas (valor, min, max, unidade) ---
    for sid, name, typ, val, vmin, vmax, unit in EXPECTED_VALUES:
        s = by[sid]
        assert (s.name, s.type, s.unit) == (name, typ, unit), sid
        assert close(s.value, val, 1e-12) and close(s.vmin, vmin, 1e-12) and close(s.vmax, vmax, 1e-12), \
            (sid, s.value, s.vmin, s.vmax)
    # selecao por categoria/nome, sem IDs fixos
    assert [round(s.value, 1) for s in T.select(tr.sensors, category="gpu", stype="SmallData")] == [774.2, 7260.2, 8034.4]
    assert len(T.select(tr.sensors, category="battery", stype="Energy")) == 3
    assert len(T.select(tr.sensors, category="cpu", stype="Temperature", name="CPU Package")) == 1
    print(f"OK test_real_sample ({len(tr.sensors)} sensores, {len(tr.hardware)} hardwares, 3 campos x 160 verificados)")


if __name__ == "__main__":
    test_parse_number_ptbr()
    test_parse_number_enus()
    test_detect_decimal()
    test_thousands_fallback_text()
    test_parse_value_units()
    test_null_is_not_zero()
    test_units_missing_or_unknown()
    test_tree_synthetic()
    test_robustness()
    test_to_rows_feeds_existing_build_readings()
    test_real_sample()
    print("OK test_lhm_tree")
# FIM test_lhm_tree.py