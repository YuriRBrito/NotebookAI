"""Nome amigavel de aplicativos e classificacao ABERTO x CAUSANDO CARGA (funcoes puras + tracker)."""
import os
import re
from dataclasses import dataclass, field

HOST_USES_TITLE = {"applicationframehost.exe", "javaw.exe", "java.exe"}


def friendly_name(exe_path: str | None, exe_name: str, title: str | None, description: str | None,
                  rules: list[tuple[str, str, str]]) -> str:
    """Regras de alias (config) -> descricao do exe -> titulo (para hosts) -> nome do exe."""
    name = (exe_name or "").lower()
    for exe_rx, title_rx, alias in rules:
        if re.search(exe_rx, name, re.I) and (not title_rx or (title and re.search(title_rx, title, re.I))):
            return alias
    if name in HOST_USES_TITLE and title:
        return title.split(" - ")[0].strip() or exe_name
    if description:
        return description
    return os.path.splitext(exe_name)[0] if exe_name else "desconhecido"


@dataclass
class LoadThresholds:
    low_below: float = 10.0       # abaixo disso (CPU e GPU) = LOW
    high_from: float = 40.0       # a partir disso (CPU ou GPU) = HIGH
    active_fg_cpu: float = 3.0    # em primeiro plano, acima disso = ACTIVE
    active_fg_gpu: float = 5.0
    active_bg_cpu: float = 10.0   # em segundo plano, acima disso = ACTIVE
    active_bg_gpu: float = 10.0
    ema_alpha: float = 0.3


def classify(cpu: float, gpu: float, foreground: bool, t: LoadThresholds) -> tuple[str, str]:
    """(estado, carga). estado: ACTIVE | OPEN. carga: LOW | MEDIUM | HIGH. Usa valores suavizados."""
    if foreground:
        active = cpu >= t.active_fg_cpu or gpu >= t.active_fg_gpu
    else:
        active = cpu >= t.active_bg_cpu or gpu >= t.active_bg_gpu
    peak = max(cpu, gpu)
    load = "HIGH" if peak >= t.high_from else ("LOW" if max(cpu, gpu) < t.low_below else "MEDIUM")
    return ("ACTIVE" if active else "OPEN"), load


@dataclass
class AppTracker:
    thresholds: LoadThresholds = field(default_factory=LoadThresholds)
    ema: dict = field(default_factory=dict)

    def update(self, groups: list[dict], fg_pid: int | None, gpu_per_pid: dict[int, float]) -> list[dict]:
        """groups: saida do ProcessCollector (data['groups']). Retorna apps classificados."""
        a = self.thresholds.ema_alpha
        out = []
        seen = set()
        for g in groups:
            key = g["key"]
            seen.add(key)
            gpu = sum(gpu_per_pid.get(p, 0.0) for p in g["pids"])
            e = self.ema.get(key)
            cpu_s = g["cpu_pct"] if e is None else e[0] + a * (g["cpu_pct"] - e[0])
            gpu_s = gpu if e is None else e[1] + a * (gpu - e[1])
            self.ema[key] = (cpu_s, gpu_s)
            is_fg = fg_pid is not None and fg_pid in g["pids"]
            state, load = classify(cpu_s, gpu_s, is_fg, self.thresholds)
            out.append({**g, "gpu_pct": min(100.0, gpu), "cpu_smooth": cpu_s, "gpu_smooth": gpu_s,
                        "foreground": is_fg, "state": state, "load": load})
        for k in list(self.ema):
            if k not in seen:
                del self.ema[k]
        return out
