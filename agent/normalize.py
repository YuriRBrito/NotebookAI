"""Normalizador: sensores do HWiNFO -> chaves canonicas. Nao mapeado -> hwinfo.<cat>.<grupo>.<sensor>.

As regras casam por regex sobre rotulo/unidade/grupo (nao por nomes exatos do meu PC).
Os rotulos reais do HWiNFO variam por CPU/driver: o --list-sensors mostra o que casou e o que nao.
"""
import re
from collections import defaultdict

from agent.readings import Reading

_T = "temp"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


def categorize(group: str) -> str:
    g = group.lower()
    if g.startswith("cpu") or "processor" in g:
        return "cpu"
    if g.startswith("gpu") or "graphics" in g:
        return "gpu"
    if g.startswith("memory") or "dimm" in g or "ddr" in g or "physical memory" in g:
        return "ram"
    if g.startswith("drive") or "s.m.a.r.t" in g or "nvme" in g:
        return "ssd"
    if g.startswith("battery") or "battery" in g:
        return "battery"
    if "whea" in g:
        return "whea"
    if "network" in g or g.startswith("wi-fi") or "ethernet" in g:
        return "network"
    if g.startswith("system") or "windows" in g:
        return "system"
    return "other"


def _is_flag(r: Reading) -> bool:
    return r.unit.strip().lower() == "yes/no"


def _w_to_w(r: Reading) -> float:
    return r.value


def _wh_to_mwh(r: Reading) -> float:
    u = r.unit.strip().lower()
    if u == "wh":
        return r.value * 1000.0
    return r.value          # mWh


# (categoria, regex do rotulo, predicado extra, chave) -- primeira regra que casa vence.
# {n} no destino = grupo 1 do regex.
RULES = [
    # --- CPU ---
    ("cpu", r"^cpu package$", lambda r: r.unit == "°C", "cpu.package_temp_c"),
    ("cpu", r"^(?:p-core|e-core|core) ?(\d+)(?: t\d+)?$", lambda r: r.unit == "°C", "cpu.core_temp_c.{n}"),
    ("cpu", r"^cpu package power$", None, "cpu.package_power_w"),
    ("cpu", r"^(?:ia cores power|cpu core power|core power)$", None, "cpu.core_power_w"),
    ("cpu", r"^(?:p-core|e-core|core) ?(\d+)(?: t\d+)? clock", lambda r: "effective" not in r.label.lower(), "cpu.core_clock_mhz.{n}"),
    ("cpu", r"^(?:p-core|e-core|core) ?(\d+)(?: t\d+)? .*effective clock|^core (\d+) .*effective", None, "cpu.core_eff_clock_mhz.{n}"),
    ("cpu", r"^average effective clock$", None, "cpu.effective_clock_mhz"),
    ("cpu", r"^total cpu usage$", None, "cpu.hwinfo.util_pct"),
    ("cpu", r"\bpl1\b|power limit ?1", None, "cpu.pl1_w"),
    ("cpu", r"\bpl2\b|power limit ?2", None, "cpu.pl2_w"),
    ("cpu", r"thermal throttl", _is_flag, "cpu.thermal_throttle"),
    ("cpu", r"power limit exceeded|power limit$", _is_flag, "cpu.power_limit"),
    ("cpu", r"current/edp|edp limit", _is_flag, "cpu.edp_limit"),
    ("cpu", r"prochot", _is_flag, "cpu.prochot"),
    # --- GPU ---
    ("gpu", r"^gpu temperature$", None, "gpu.temp_c"),
    ("gpu", r"hot ?spot", None, "gpu.hotspot_c"),
    ("gpu", r"^gpu (?:core load|utilization|usage)$", None, "gpu.hwinfo.util_pct"),
    ("gpu", r"^gpu clock$", None, "gpu.clock_mhz"),
    ("gpu", r"^gpu (?:total )?(?:power|chip power)$|^gpu power$", None, "gpu.power_w"),
    ("gpu", r"memory (?:usage|used|dynamic)|d3d memory dynamic", None, "gpu.hwinfo.mem_used"),
    ("gpu", r"throttl|performance limit", _is_flag, "gpu.thermal_throttle"),
    # --- RAM ---
    ("ram", r"^memory clock$", None, "ram.hwinfo.memory_clock_mhz"),
    # --- SSD (indice do drive entra como sufixo) ---
    ("ssd", r"^drive temperature$", None, "{d}.temp_c"),
    ("ssd", r"remaining life", None, "{d}.life_remaining_pct"),
    ("ssd", r"total host writes", None, "{d}.host_writes"),
    ("ssd", r"total host reads", None, "{d}.host_reads"),
    ("ssd", r"^read rate$", None, "{d}.hwinfo.read_rate"),
    ("ssd", r"^write rate$", None, "{d}.hwinfo.write_rate"),
    ("ssd", r"^(?:drive failure|drive warning)$", _is_flag, "{d}.smart_flag_{s}"),
    # --- Bateria (valores do HWiNFO ficam separados dos da Battery API: validacao cruzada) ---
    ("battery", r"design(?:ed)? capacity", None, "battery.hwinfo.design_mwh"),
    ("battery", r"full charge[d]? capacity", None, "battery.hwinfo.full_mwh"),
    ("battery", r"remaining capacity|current capacity", None, "battery.hwinfo.remaining_mwh"),
    ("battery", r"battery voltage|^voltage$", None, "battery.hwinfo.voltage_v"),
    ("battery", r"charge level", None, "battery.hwinfo.charge_pct"),
    ("battery", r"wear level|battery wear", None, "battery.hwinfo.wear_pct"),
    ("battery", r"charge rate|discharge rate|^charge/discharge", None, "battery.hwinfo.rate_w"),
    # --- WHEA ---
    ("whea", r".*", None, "whea.{s}"),
]

_AGG_MAX = {"cpu.thermal_throttle", "cpu.power_limit", "cpu.edp_limit", "cpu.prochot", "gpu.thermal_throttle"}


def map_hwinfo(readings: list[Reading], sensor_names: list[str]) -> list[Reading]:
    """Atribui chave a cada leitura e agrega flags. Mantem tudo que nao casou."""
    drive_index: dict[str, int] = {}
    out: list[Reading] = []
    used: dict[str, int] = defaultdict(int)
    flags: dict[str, Reading] = {}

    for r in readings:
        group = r.group or r.category
        cat = categorize(group)
        label = r.label.strip()
        low = label.lower()
        key = None
        for rcat, rx, pred, dest in RULES:
            if rcat != cat:
                continue
            m = re.search(rx, low)
            if not m or (pred and not pred(r)):
                continue
            n = next((g for g in m.groups() if g is not None), "")
            if "{d}" in dest:
                idx = drive_index.setdefault(group, len(drive_index))
                dest = dest.replace("{d}", "ssd" if idx == 0 else f"ssd{idx}")
            key = dest.replace("{n}", n).replace("{s}", slug(label))
            break

        mapped = key is not None
        if not mapped:
            key = f"hwinfo.{cat}.{slug(group)}.{slug(label)}"
        if key in _AGG_MAX:
            prev = flags.get(key)
            if prev is None or (r.value or 0) > (prev.value or 0):
                r.key, r.category = key, cat
                r.note = f"max de varios sensores HWiNFO ({label})"
                flags[key] = r
            continue
        if used[key]:                                  # duplicata: desambigua pelo reading id
            key = f"{key}#{(r.sensor_id or '').rsplit(':', 1)[-1] or used[key]}"
        used[key] += 1
        if mapped and key.startswith("battery.hwinfo.") and key.endswith("_mwh"):
            r.value = _wh_to_mwh(r)
            r.unit = "mWh"
        r.key, r.category = key, cat
        r.kind = "measured"
        out.append(r)
        r.note = ("" if mapped else "nao mapeado para chave canonica") + (f" [{r.note}]" if r.note and not mapped else "")
        r.note = r.note.strip()
    out.extend(flags.values())

    # clock "atual" = media calculada dos clocks por nucleo (rotulado como calculado)
    clocks = [r.value for r in out if re.fullmatch(r"cpu\.core_clock_mhz\.\w+", r.key) and r.value is not None]
    if clocks:
        out.append(Reading(key="cpu.clock_mhz", value=sum(clocks) / len(clocks), unit="MHz",
                           source="HWiNFO (media dos clocks por nucleo)", label="CPU clock (media)",
                           kind="calculated", category="cpu",
                           note=f"media de {len(clocks)} clocks por nucleo informados pelo HWiNFO"))
    return out
