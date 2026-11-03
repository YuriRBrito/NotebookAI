"""Janela em primeiro plano -> PID, processo, caminho, nome amigavel. Generico (nao depende de jogo)."""
import os

import psutil

from agent import winapi
from agent.apps import friendly_name
from agent.readings import Collector, CollectorResult

DEFAULT_RULES = [(r"^javaw?\.exe$", r"^Minecraft", "Minecraft Java")]


class ForegroundCollector(Collector):
    name = "Foreground"
    admin_expected = "nao (processos elevados podem negar o caminho; ha fallback por QueryLimitedInformation)"

    def __init__(self, rules=None):
        self.rules = rules if rules is not None else DEFAULT_RULES

    def collect(self) -> CollectorResult:
        winapi.require_windows()
        res = CollectorResult(self.name)
        fw = winapi.foreground_window()
        if not fw:
            res.data = {"foreground": None, "note": "nenhuma janela em primeiro plano (tela bloqueada/desktop)"}
            return res
        pid = fw["pid"]
        path, pname, notes = None, None, []
        try:
            p = psutil.Process(pid)
            pname = p.name()
            try:
                path = p.exe() or None
            except (psutil.AccessDenied, OSError):
                notes.append("psutil negou o caminho")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            notes.append(f"psutil: {type(e).__name__}")
        if not path:
            path = winapi.process_image_path(pid)
            if path:
                notes.append("caminho obtido via QueryFullProcessImageName")
        exe_name = os.path.basename(path) if path else (pname or "?")
        desc = winapi.file_description(path) if path else None
        friendly = friendly_name(path, exe_name, fw["title"], desc, self.rules)
        res.data = {"foreground": {"friendly": friendly, "exe": exe_name, "pid": pid, "path": path,
                                   "title": fw["title"], "minimized": fw["minimized"],
                                   "description": desc, "notes": notes}}
        return res
