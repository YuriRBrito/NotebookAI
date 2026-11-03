"""Todos os processos (infra completa); agrupa por executavel (Chrome = soma dos filhos)."""
import os

import psutil

from agent import winapi
from agent.apps import friendly_name
from agent.readings import Collector, CollectorResult

MB = 1024 ** 2
# Nao sao aplicativos: ficam fora do ranking (PID 0 = tempo ocioso, nao consumo).
SPECIAL_NAMES = {"system idle process", "system", "registry", "memory compression", "secure system"}
SPECIAL_PIDS = {0, 4}


class ProcessCollector(Collector):
    name = "Processes"
    admin_expected = "nao (processos protegidos/de outro usuario ficam inacessiveis e sao contados)"

    def __init__(self, rules=None):
        from agent.collectors.foreground import DEFAULT_RULES
        self.rules = rules if rules is not None else DEFAULT_RULES
        self.procs: dict[int, psutil.Process] = {}
        self.meta: dict[int, dict] = {}
        self.ncpu = psutil.cpu_count(logical=True) or 1

    def _meta_for(self, p: psutil.Process) -> dict:
        d = {"name": "?", "path": None}
        try:
            d["name"] = p.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        try:
            d["path"] = p.exe() or None
        except (psutil.AccessDenied, OSError):
            pass
        except psutil.NoSuchProcess:
            return d
        if not d["path"] and winapi.IS_WIN:
            d["path"] = winapi.process_image_path(p.pid)
        return d

    def collect(self) -> CollectorResult:
        winapi.require_windows()
        res = CollectorResult(self.name)
        alive = set(psutil.pids())
        for pid in list(self.procs):
            if pid not in alive:
                self.procs.pop(pid, None)
                self.meta.pop(pid, None)
        denied = 0
        idle_cpu = None
        specials: dict[str, float] = {}
        windows = winapi.top_level_windows()
        groups: dict[str, dict] = {}
        for pid in alive:
            p = self.procs.get(pid)
            first = p is None
            if first:
                try:
                    p = psutil.Process(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                self.procs[pid] = p
                self.meta[pid] = self._meta_for(p)
            m = self.meta[pid]
            try:
                with p.oneshot():
                    cpu = p.cpu_percent(None) / self.ncpu       # % do total de CPU
                    ws = p.memory_info().rss / MB
            except psutil.NoSuchProcess:
                self.procs.pop(pid, None)
                continue
            except (psutil.AccessDenied, OSError):
                denied += 1
                continue
            if first:
                continue                                         # 1a leitura de cpu_percent nao e valida
            if pid in SPECIAL_PIDS or m["name"].lower() in SPECIAL_NAMES:
                if pid == 0:
                    idle_cpu = min(100.0, cpu)                   # metrica separada: tempo ocioso
                else:
                    specials[m["name"]] = cpu
                continue
            key = (m["path"] or m["name"]).lower()
            g = groups.get(key)
            if g is None:
                path = m["path"]
                exe = os.path.basename(path) if path else m["name"]
                desc = winapi.file_description(path) if path else None
                g = groups[key] = {"key": key, "exe": exe, "path": path, "pids": [], "cpu_pct": 0.0,
                                   "ws_mb": 0.0, "windows_visible": 0, "windows_minimized": 0,
                                   "friendly": friendly_name(path, exe, None, desc, self.rules),
                                   "description": desc}
            g["pids"].append(pid)
            g["cpu_pct"] += cpu
            g["ws_mb"] += ws
            w = windows.get(pid)
            if w:
                g["windows_visible"] += w["visible"]
                g["windows_minimized"] += w["minimized"]
        res.data = {"groups": list(groups.values()), "total_processes": len(alive), "denied": denied,
                    "idle_cpu_pct": idle_cpu, "special_cpu": specials}
        return res
