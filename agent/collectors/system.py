"""CPU, RAM, GPU e SSD via Windows (psutil, PDH, GetPerformanceInfo, CIM).
Cada coletor e independente. Sensor ausente -> sem leitura (o diagnostico mostra N/D e o motivo);
nada e substituido em silencio por outra metrica."""
import re

import psutil

from agent import winapi
from agent.collectors import static_info
from agent.readings import Collector, CollectorResult, Reading, Unavailable

GB = 1024 ** 3
MB = 1024 ** 2
SRC_PSUTIL = "psutil (Windows)"
SRC_PDH = "Windows PDH"
SRC_PERF = "Windows GetPerformanceInfo"


class PdhSet:
    """Um query PDH por coletor. Contadores que nao existem ficam registrados em .failed."""

    def __init__(self, paths: list[str]):
        self.q = winapi.PDHQuery()
        self.failed: dict[str, str] = {}
        for p in paths:
            if not self.q.add(p):
                self.failed[p] = self.q.errors.get(p, "?")
        self.q.collect()                      # primeira coleta: prepara contadores de taxa

    def sample(self):
        self.q.collect()

    def val(self, p):
        return self.q.value(p)

    def arr(self, p):
        return self.q.array(p)


def _static():
    try:
        return static_info.get()
    except Unavailable:
        return None


# ----------------------------------------------------------------------
class CpuCollector(Collector):
    name = "Windows CPU"
    admin_expected = "nao"

    P_FREQ = r"\Processor Information(_Total)\Processor Frequency"

    def __init__(self):
        psutil.cpu_percent(None)             # prime
        self.pdh = None

    def _cpu_name(self):
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            return None

    def collect(self):
        winapi.require_windows()
        res = CollectorResult(self.name)
        if self.pdh is None:
            self.pdh = PdhSet([self.P_FREQ])
        self.pdh.sample()
        res.readings.append(Reading("cpu.util_pct", psutil.cpu_percent(None), "%", SRC_PSUTIL, "CPU utilization"))
        f = self.pdh.val(self.P_FREQ)
        if f is not None:
            res.readings.append(Reading(
                "cpu.windows.freq_mhz", f, "MHz", SRC_PDH, "Processor Frequency (Windows)", kind="calculated",
                note="derivado pelo Windows (base x % de desempenho); pode divergir do clock real"))
        try:
            fr = psutil.cpu_freq()
            if fr:
                res.readings.append(Reading(
                    "cpu.psutil.freq_mhz", fr.current, "MHz", SRC_PSUTIL, "cpu_freq().current", kind="reported",
                    note="no Windows costuma ser a frequencia nominal, nao a instantanea"))
        except Exception:
            pass
        res.data = {"cpu_name": self._cpu_name(), "logical": psutil.cpu_count(logical=True),
                    "physical": psutil.cpu_count(logical=False), "pdh_failed": dict(self.pdh.failed)}
        st = _static()
        if st and st.get("cpu"):
            res.data["cim"] = st["cpu"][0]
        return res


# ----------------------------------------------------------------------
class RamCollector(Collector):
    name = "Windows RAM"
    admin_expected = "nao"
    P_FREE = r"\Memory\Free & Zero Page List Bytes"
    P_PF = r"\Paging File(_Total)\% Usage"

    def __init__(self):
        self.pdh = None

    def collect(self):
        winapi.require_windows()
        res = CollectorResult(self.name)
        if self.pdh is None:
            self.pdh = PdhSet([self.P_FREE, self.P_PF])
        self.pdh.sample()
        pi = winapi.performance_info()
        used = pi["phys_total"] - pi["phys_avail"]
        R = res.readings.append
        R(Reading("ram.total_gb", pi["phys_total"] / GB, "GB", SRC_PERF, "Total"))
        R(Reading("ram.used_gb", used / GB, "GB", SRC_PERF, "Used", note="total - disponivel"))
        R(Reading("ram.available_gb", pi["phys_avail"] / GB, "GB", SRC_PERF, "Available"))
        R(Reading("ram.used_pct", used / pi["phys_total"] * 100, "%", SRC_PERF, "Usage", kind="calculated"))
        R(Reading("ram.commit_gb", pi["commit_total"] / GB, "GB", SRC_PERF, "Committed"))
        R(Reading("ram.commit_limit_gb", pi["commit_limit"] / GB, "GB", SRC_PERF, "Commit limit"))
        R(Reading("ram.commit_pct", pi["commit_total"] / pi["commit_limit"] * 100, "%", SRC_PERF,
                  "Commit %", kind="calculated"))
        free = self.pdh.val(self.P_FREE)
        if free is not None:
            R(Reading("ram.free_gb", free / GB, "GB", SRC_PDH, "Free",
                      note="Free & Zero Page List (exclui cache em standby)"))
        pf = self.pdh.val(self.P_PF)
        if pf is not None:
            R(Reading("ram.pagefile_usage_pct", pf, "%", SRC_PDH, "Pagefile usage"))
        st = _static()
        mem = (st or {}).get("mem") or []
        if mem:
            conf = [m.get("ConfiguredClockSpeed") for m in mem if m.get("ConfiguredClockSpeed")]
            spd = [m.get("Speed") for m in mem if m.get("Speed")]
            if conf:
                R(Reading("ram.freq_mts", float(max(conf)), "MT/s", static_info.SRC, "Configured speed",
                          kind="reported", note="informado pelo firmware (SMBIOS); nao e medido em tempo real"))
            elif spd:
                R(Reading("ram.freq_mts", float(max(spd)), "MT/s", static_info.SRC, "Speed (nominal)",
                          kind="reported", note="velocidade nominal do modulo; ConfiguredClockSpeed ausente"))
        pfs = (st or {}).get("pagefile") or []
        if pfs:
            size = sum(p.get("AllocatedBaseSize") or 0 for p in pfs)
            R(Reading("ram.pagefile_size_mb", float(size), "MB", static_info.SRC, "Pagefile size",
                      kind="reported", note="tamanho alocado no momento da leitura inicial"))
            if pf is not None and size:
                R(Reading("ram.pagefile_used_mb", pf / 100 * size, "MB", "Windows PDH + CIM", "Pagefile used",
                          kind="calculated", note="% de uso (PDH) x tamanho alocado (CIM)"))
        res.data = {"dimms": mem, "pagefiles": pfs, "pdh_failed": dict(self.pdh.failed)}
        return res


# ----------------------------------------------------------------------
_ENG = re.compile(r"pid_(\d+)_luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)_phys_\d+_eng_\d+_engtype_(.+)$")


def aggregate_gpu_engines(items: dict[str, float]) -> tuple[float, dict[str, float], dict[int, float]]:
    """(util_total, por_tipo, por_pid). util = maior soma por tipo de engine (como o Gerenciador de Tarefas)."""
    by_type: dict[str, float] = {}
    by_pid_type: dict[int, dict[str, float]] = {}
    for name, v in items.items():
        m = _ENG.search(name)
        if not m:
            continue
        pid, _luid, et = int(m.group(1)), m.group(2), m.group(3)
        by_type[et] = by_type.get(et, 0.0) + v
        d = by_pid_type.setdefault(pid, {})
        d[et] = d.get(et, 0.0) + v
    util = min(100.0, max(by_type.values())) if by_type else 0.0
    per_pid = {pid: min(100.0, max(d.values())) for pid, d in by_pid_type.items()}
    return util, by_type, per_pid


class GpuCollector(Collector):
    name = "Windows GPU"
    admin_expected = "nao"
    P_ENG = r"\GPU Engine(*)\Utilization Percentage"
    P_SH = r"\GPU Adapter Memory(*)\Shared Usage"
    P_DED = r"\GPU Adapter Memory(*)\Dedicated Usage"

    def __init__(self):
        self.pdh = None

    def collect(self):
        winapi.require_windows()
        res = CollectorResult(self.name)
        if self.pdh is None:
            self.pdh = PdhSet([self.P_ENG, self.P_SH, self.P_DED])
        if self.P_ENG in self.pdh.failed:
            raise Unavailable(f"contador 'GPU Engine' indisponivel (erro {self.pdh.failed[self.P_ENG]}); "
                              "driver WDDM 2.x+ necessario")
        self.pdh.sample()
        items = self.pdh.arr(self.P_ENG) or {}
        util, by_type, per_pid = aggregate_gpu_engines(items)
        res.readings.append(Reading("gpu.util_pct", util, "%", SRC_PDH, "GPU utilization", kind="calculated",
                                    note="maior soma por tipo de engine (3D, Copy, Video...)"))
        for et, v in by_type.items():
            res.readings.append(Reading(f"gpu.engine.{re.sub(r'[^a-z0-9]+', '_', et.lower())}_pct", min(100.0, v),
                                        "%", SRC_PDH, f"Engine {et}"))
        for key, p, lab in (("gpu.shared_mem_mb", self.P_SH, "Shared memory used"),
                            ("gpu.dedicated_mem_mb", self.P_DED, "Dedicated memory used")):
            a = self.pdh.arr(p)
            if a is not None:
                res.readings.append(Reading(key, sum(a.values()) / MB, "MB", SRC_PDH, lab))
        st = _static()
        res.data = {"per_pid": per_pid, "adapters": (st or {}).get("gpu") or [],
                    "pdh_failed": dict(self.pdh.failed)}
        return res


# ----------------------------------------------------------------------
class StorageCollector(Collector):
    name = "Windows SSD"
    admin_expected = "nao para atividade/vazao; saude/SMART pode exigir elevacao (a verificar)"
    P_BUSY = r"\PhysicalDisk(_Total)\% Disk Time"
    P_R = r"\PhysicalDisk(_Total)\Disk Read Bytes/sec"
    P_W = r"\PhysicalDisk(_Total)\Disk Write Bytes/sec"

    def __init__(self):
        self.pdh = None

    def collect(self):
        winapi.require_windows()
        res = CollectorResult(self.name)
        if self.pdh is None:
            self.pdh = PdhSet([self.P_BUSY, self.P_R, self.P_W])
        self.pdh.sample()
        b, r, w = self.pdh.val(self.P_BUSY), self.pdh.val(self.P_R), self.pdh.val(self.P_W)
        if b is not None:
            res.readings.append(Reading("ssd.activity_pct", min(100.0, b), "%", SRC_PDH, "Activity (% Disk Time)",
                                        note="PhysicalDisk(_Total), todos os discos" +
                                             (f"; valor bruto {b:.0f}% limitado a 100" if b > 100 else "")))
        if r is not None:
            res.readings.append(Reading("ssd.read_mbps", r / MB, "MB/s", SRC_PDH, "Read"))
        if w is not None:
            res.readings.append(Reading("ssd.write_mbps", w / MB, "MB/s", SRC_PDH, "Write"))
        st = _static()
        disks = (st or {}).get("disks") or []
        for d in disks[:1]:
            if d.get("Size"):
                res.readings.append(Reading("ssd.capacity_gb", d["Size"] / 1e9, "GB", static_info.SRC,
                                            "Capacity", kind="reported", note="Get-PhysicalDisk (GB decimais)"))
        res.data = {"disks": disks, "pdh_failed": dict(self.pdh.failed)}
        return res
