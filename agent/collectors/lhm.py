"""LibreHardwareMonitor (substitui o HWiNFO).

Fonte primaria: WMI publicado pelo proprio LibreHardwareMonitor enquanto ele esta aberto
(namespace root/LibreHardwareMonitor, classe Sensor). Valores numericos, sem depender de locale.
Fonte alternativa: servidor web do LHM (http://localhost:8085/data.json), usada so se o WMI falhar.

NAO VALIDADO NO SEU PC: nomes de sensores variam por CPU/driver; o --list-sensors mostra o que casou.
Erros sao reportados com o texto real (stderr do PowerShell / excecao), sem mascarar.
"""
import json
import re
import subprocess
import urllib.error
import urllib.request

import psutil

from agent import winapi
from agent.readings import Collector, CollectorResult, Reading, Unavailable

NAMESPACES = ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor")
PS_TMPL = ("$ErrorActionPreference='Stop';"
           "Get-CimInstance -Namespace {ns} -ClassName Sensor | "
           "Select-Object Identifier,Name,SensorType,Value,Min,Max,Parent | ConvertTo-Json -Compress -Depth 2")
UNITS = {"Temperature": "°C", "Load": "%", "Clock": "MHz", "Power": "W", "Voltage": "V", "Current": "A",
         "Data": "GB", "SmallData": "MB", "Throughput": "B/s", "Energy": "mWh", "Level": "%",
         "Fan": "RPM", "Frequency": "Hz", "TimeSpan": "s", "Factor": "", "Flow": "L/h", "Control": "%",
         "Noise": "dBA"}
CAT = {"intelcpu": "cpu", "amdcpu": "cpu", "cpu": "cpu", "gpu-intel": "gpu", "gpu-nvidia": "gpu", "gpu-amd": "gpu",
       "intelgpu": "gpu", "nvme": "ssd", "hdd": "ssd", "ssd": "ssd", "ram": "ram", "vram": "ram",
       "battery": "battery", "lpc": "other", "mainboard": "other", "nic": "network", "psu": "other"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


def hw_of(identifier: str) -> tuple[str, str]:
    """'/intelcpu/0/temperature/3' -> ('intelcpu', 'intelcpu_0')"""
    parts = [p for p in identifier.split("/") if p]
    return (parts[0] if parts else "?"), "_".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else "?")


def canonical(identifier: str, name: str, stype: str) -> str | None:
    hw, _ = hw_of(identifier)
    cat = CAT.get(hw, "other")
    n = name.lower()
    m = re.search(r"core\s*#?(\d+)", n)
    if cat == "cpu":
        if stype == "Temperature":
            if n == "cpu package":
                return "cpu.package_temp_c"
            if m and "distance" not in n:
                return f"cpu.core_temp_c.{m.group(1)}"
        if stype == "Load" and n == "cpu total":
            return "cpu.lhm.util_pct"
        if stype == "Clock" and m:
            return f"cpu.core_clock_mhz.{m.group(1)}"
        if stype == "Power":
            if n == "cpu package":
                return "cpu.package_power_w"
            if n in ("cpu cores", "cpu core", "core"):
                return "cpu.core_power_w"
    elif cat == "gpu":
        if stype == "Load" and n in ("gpu core", "d3d 3d"):
            return "gpu.lhm.util_pct"
        if stype == "Temperature" and n in ("gpu core", "gpu"):
            return "gpu.temp_c"
        if stype == "Temperature" and "hot" in n:
            return "gpu.hotspot_c"
        if stype == "Clock" and n == "gpu core":
            return "gpu.clock_mhz"
        if stype == "Power" and ("gpu" in n or "package" in n):
            return "gpu.power_w"
        if "shared" in n and "used" in n:
            return "gpu.lhm.shared_mem"
    elif cat == "ssd":
        if stype == "Temperature" and n in ("temperature", "composite", "temperature 1"):
            return "ssd.temp_c"
        if stype == "Level" and n == "percentage used":
            return "ssd.lhm.percentage_used"
        if stype == "Level" and n == "available spare":
            return "ssd.lhm.available_spare_pct"
    elif cat == "ram":
        if hw == "ram" and stype == "Load" and n == "memory":
            return "ram.lhm.util_pct"
        if hw == "ram" and stype == "Data" and n == "memory used":
            return "ram.lhm.used_gb"
        if hw == "ram" and stype == "Data" and n == "memory available":
            return "ram.lhm.available_gb"
    elif cat == "battery":
        table = {("Voltage", "battery"): "battery.lhm.voltage_v", ("Level", "charge level"): "battery.lhm.charge_pct",
                 ("Level", "degradation level"): "battery.lhm.wear_pct",
                 ("Energy", "designed capacity"): "battery.lhm.design_mwh",
                 ("Energy", "full charged capacity"): "battery.lhm.full_mwh",
                 ("Energy", "remaining capacity"): "battery.lhm.remaining_mwh",
                 ("Power", "charge/discharge rate"): "battery.lhm.rate_w"}
        return table.get((stype, n))
    return None


def build_readings(rows: list[dict], source: str) -> list[Reading]:
    out: list[Reading] = []
    used: dict[str, int] = {}
    hw_names: dict[str, str] = {}
    for r in rows:
        ident, name, st = str(r.get("Identifier") or ""), str(r.get("Name") or ""), str(r.get("SensorType") or "")
        hw, hw_slug = hw_of(ident)
        cat = CAT.get(hw, "other")
        val = r.get("Value")
        key = canonical(ident, name, st)
        mapped = key is not None
        if not mapped:
            key = f"lhm.{cat}.{hw_slug}.{slug(name)}.{slug(st)}"
        if used.get(key):
            key = f"{key}#{ident.rsplit('/', 1)[-1]}"
        used[key] = used.get(key, 0) + 1
        unit = UNITS.get(st, "")
        value = None if val is None else float(val)
        note = "" if value is not None else "LibreHardwareMonitor listou o sensor sem valor (null)"
        if not mapped and value is not None:
            note = "nao mapeado para chave canonica"
        out.append(Reading(key=key, value=value, unit=unit, source=source, label=name,
                           vmin=None if r.get("Min") is None else float(r["Min"]),
                           vmax=None if r.get("Max") is None else float(r["Max"]),
                           sensor_id=ident, category=cat, group=str(r.get("Parent") or hw_slug), note=note))
    # clock atual = media dos clocks por nucleo (calculado)
    clocks = [x.value for x in out if re.fullmatch(r"cpu\.core_clock_mhz\.\d+", x.key) and x.value is not None]
    if clocks:
        out.append(Reading(key="cpu.clock_mhz", value=sum(clocks) / len(clocks), unit="MHz",
                           source=source + " (media dos clocks por nucleo)", label="CPU clock (media)",
                           kind="calculated", category="cpu",
                           note=f"media de {len(clocks)} clocks por nucleo"))
    # vida restante do SSD = 100 - Percentage Used (calculado)
    pu = next((x for x in out if x.key == "ssd.lhm.percentage_used" and x.value is not None), None)
    if pu:
        out.append(Reading(key="ssd.life_remaining_pct", value=100.0 - pu.value, unit="%",
                           source=source + " (calculado)", label="Vida restante (100 - Percentage Used)",
                           kind="calculated", category="ssd"))
    return out


def lhm_running() -> bool:
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith("librehardwaremonitor"):
            return True
    return False


class LhmCollector(Collector):
    name = "LibreHardwareMonitor"
    admin_expected = ("o LHM costuma precisar de administrador para ler CPU/SSD; o Agent so le WMI/HTTP "
                      "e pode ficar sem elevacao (a verificar)")

    def __init__(self, http_url: str = "http://localhost:8085/data.json"):
        self.http_url = http_url
        self.ns: str | None = None
        self.stages: list[tuple[str, str, str]] = []

    # --- fontes ---------------------------------------------------------
    def _wmi(self) -> list[dict]:
        errs = []
        for ns in ([self.ns] if self.ns else NAMESPACES):
            try:
                cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                     PS_TMPL.format(ns=ns)], capture_output=True, text=True, timeout=30,
                                    creationflags=0x08000000)
            except FileNotFoundError:
                raise Unavailable("powershell.exe nao encontrado")
            except subprocess.TimeoutExpired:
                raise Unavailable("PowerShell excedeu 30 s consultando o WMI do LibreHardwareMonitor")
            if cp.returncode == 0 and cp.stdout.strip():
                self.ns = ns
                data = json.loads(cp.stdout)
                return data if isinstance(data, list) else [data]
            errs.append(f"{ns}: rc={cp.returncode} {(cp.stderr or 'sem saida').strip()[:220]}")
        raise Unavailable("WMI: " + " | ".join(errs))

    def _http(self) -> list[dict]:
        try:
            with urllib.request.urlopen(self.http_url, timeout=3) as r:
                tree = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as e:
            raise Unavailable(f"HTTP {self.http_url}: {e}")
        rows: list[dict] = []

        def walk(node, parent=""):
            if "SensorId" in node and "Type" in node:
                v = re.search(r"-?\d+(?:[.,]\d+)?", str(node.get("Value", "")))
                rows.append({"Identifier": node["SensorId"], "Name": node.get("Text", ""),
                             "SensorType": node["Type"], "Parent": parent,
                             "Value": float(v.group().replace(",", ".")) if v else None,
                             "Min": None, "Max": None})
            for c in node.get("Children", []) or []:
                walk(c, node.get("Text", parent))

        walk(tree)
        if not rows:
            raise Unavailable("HTTP respondeu, mas sem 'SensorId'/'Type' (versao do LHM sem esses campos?)")
        return rows

    def collect(self) -> CollectorResult:
        winapi.require_windows()
        running = lhm_running()
        try:
            rows, src = self._wmi(), "LibreHardwareMonitor (WMI)"
        except Unavailable as e_wmi:
            try:
                rows, src = self._http(), "LibreHardwareMonitor (HTTP)"
            except Unavailable as e_http:
                why = "LibreHardwareMonitor.exe NAO esta em execucao; " if not running else ""
                raise Unavailable(f"{why}{e_wmi}; {e_http}")
        readings = build_readings(rows, src)
        hw = {r.group for r in readings}
        return CollectorResult(self.name, readings=readings, data={
            "n_sensors": len(hw), "n_readings": len(readings), "version": src, "stale_note": "",
            "process_running": running})
