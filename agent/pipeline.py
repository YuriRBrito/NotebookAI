"""Orquestra os coletores com cadencias diferentes e monta o Frame/Sample normalizado."""
import time
from dataclasses import dataclass, field

from agent import winapi
from agent.apps import AppTracker
from agent.collectors.battery import BatteryCollector
from agent.collectors.eventlog import EventLogCollector
from agent.collectors.foreground import ForegroundCollector
from agent.collectors.lhm_live import LhmPollerCollector
from agent.collectors.processes import ProcessCollector
from agent.collectors.system import CpuCollector, GpuCollector, RamCollector, StorageCollector
from agent.intervals import Sample
from agent.readings import CollectorResult, Reading
from common.config import Config


@dataclass
class Frame:
    ts: float
    readings: dict[str, Reading] = field(default_factory=dict)
    results: dict[str, CollectorResult] = field(default_factory=dict)   # ultimo resultado de cada coletor
    ran: set = field(default_factory=set)                              # coletores executados neste tick
    apps: list[dict] = field(default_factory=list)
    foreground: dict | None = None
    events: list[dict] = field(default_factory=list)                   # eventos novos do Event Log
    status_changes: list[tuple] = field(default_factory=list)          # (nome, antes, depois, motivo)
    elevated: bool = False


class Pipeline:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()
        c = self.cfg
        self.collectors = {
                        "LibreHardwareMonitor": LhmPollerCollector(c.lhm_url, interval_s=c.lhm_poll_secs,
                                                       timeout_s=c.lhm_timeout_secs,
                                                       stale_after_s=c.lhm_stale_secs),
            "Windows CPU": CpuCollector(),
            "Windows RAM": RamCollector(),
            "Windows GPU": GpuCollector(),
            "Windows SSD": StorageCollector(),
            "Battery API": BatteryCollector(c.battery_nominal_voltage_mv, c.battery_reference_design_mwh),
            "Foreground": ForegroundCollector(),
            "Processes": ProcessCollector(),
            "Event Log": EventLogCollector(),
        }
        self.every = {"Processes": c.process_secs, "Event Log": c.eventlog_secs, "LibreHardwareMonitor": 3.0}
        self.last_run: dict[str, float] = {}
        self.last: dict[str, CollectorResult] = {}
        self.prev_status: dict[str, str] = {}
        self.tracker = AppTracker()
        self.elevated = winapi.is_elevated()

            # --- ciclo de vida: coletores com thread propria (hoje so o LHM) -------------------------------
    def start(self, wait_first_s: float = 0.0) -> None:
        """Inicia (uma vez, idempotente) os coletores que tem start(). wait_first_s > 0: espera limitada pela
        1a leitura (so para o CLI, ANTES do laco; o tick() nunca espera)."""
        for col in self.collectors.values():
            fn = getattr(col, "start", None)
            if fn:
                fn()
        if wait_first_s > 0:
            for col in self.collectors.values():
                fn = getattr(col, "prime", None)
                if fn:
                    fn(wait_first_s)

    def close(self) -> None:
        """Encerra os coletores que tem close(). Idempotente; nunca levanta."""
        for col in self.collectors.values():
            fn = getattr(col, "close", None)
            if fn:
                try:
                    fn()
                except Exception:  # noqa: BLE001 - encerrar nunca pode falhar o chamador
                    pass

    def __enter__(self) -> "Pipeline":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def tick(self, now: float | None = None, force_all: bool = False) -> Frame:
        now = now or time.time()
        f = Frame(ts=now, elevated=self.elevated)
        for name, col in self.collectors.items():
            due = force_all or (now - self.last_run.get(name, 0.0)) >= self.every.get(name, 0.0) - 0.05
            if not due:
                continue
            res = col.run()
            self.last_run[name] = now
            self.last[name] = res
            f.ran.add(name)
            old = self.prev_status.get(name)
            if old != res.status:
                f.status_changes.append((name, old, res.status, res.reason))
                self.prev_status[name] = res.status
        f.results = dict(self.last)
        for name in f.ran:
            r = self.last[name]
            if r.status == "OK":
                for rd in r.readings:
                    f.readings.setdefault(rd.key, rd)
        # --- primeiro plano + aplicativos classificados ---
        fgr = self.last.get("Foreground")
        if fgr and fgr.status == "OK":
            f.foreground = fgr.data.get("foreground")
        pr = self.last.get("Processes")
        gr = self.last.get("Windows GPU")
        if pr and pr.status == "OK":
            gpu_pp = gr.data.get("per_pid", {}) if gr and gr.status == "OK" else {}
            fg_pid = f.foreground["pid"] if f.foreground else None
            f.apps = self.tracker.update(pr.data.get("groups", []), fg_pid, gpu_pp)
            if f.foreground:       # nome amigavel do foreground usa o titulo da janela (alias de jogos etc.)
                for a in f.apps:
                    if a["foreground"]:
                        a["friendly"] = f.foreground["friendly"]
        er = self.last.get("Event Log")
        if er and "Event Log" in f.ran and er.status == "OK":
            f.events = er.data.get("events", [])
        return f


def to_sample(f: Frame) -> Sample:
    """Frame -> Sample para o motor de intervalos. Somente valores reais (None e ignorado)."""
    values: dict[str, float] = {}
    meta: dict[str, dict] = {}
    for k, r in f.readings.items():
        if r.value is None:
            continue
        values[k] = float(r.value)
        meta[k] = {"label": r.label, "unit": r.unit, "source": r.source}
    fg = f.foreground
    discrete = {"state.fg_app": fg["friendly"] if fg else "(nenhum)"}
    ac = f.readings.get("battery.ac")
    if ac is not None and ac.value is not None:
        discrete["state.ac"] = "1" if ac.value else "0"
    if fg:
        for a in f.apps:
            if a["foreground"]:
                values["app.fg_cpu_pct"] = a["cpu_pct"]
                values["app.fg_gpu_pct"] = a["gpu_pct"]
                values["app.fg_ram_mb"] = a["ws_mb"]
                break
    return Sample(f.ts, values, discrete, meta)
