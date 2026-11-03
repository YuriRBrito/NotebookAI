"""TXT diario legivel por IA. Anexa intervalos fechados e eventos em ordem."""
import json
import time
from pathlib import Path

# chave -> (rotulo, unidade, casas decimais)
LINES = [
    ("ram.used_gb", "RAM", "GB", 1),
    ("cpu.util_pct", "CPU", "%", 0),
    ("gpu.util_pct", "GPU", "%", 0),
    ("cpu.package_temp_c", "CPU Temp", "°C", 0),
    ("gpu.temp_c", "GPU Temp", "°C", 0),
    ("cpu.package_power_w", "CPU Potencia", "W", 1),
    ("cpu.clock_mhz", "CPU Clock", "MHz", 0),
    ("fps", "FPS", "", 0),
    ("frametime_ms", "Frametime", "ms", 1),
    ("battery.pct", "Bateria", "%", 0),
    ("battery.power_w", "Bateria potencia", "W", 1),
    ("ssd.activity_pct", "SSD atividade", "%", 0),
    ("app.fg_cpu_pct", "App em 1o plano CPU", "%", 0),
    ("app.fg_gpu_pct", "App em 1o plano GPU", "%", 0),
]


def _n(x: float, d: int) -> str:
    return f"{x:.{d}f}".replace(".", ",")


def _rng(st: dict, d: int, unit: str) -> str:
    lo, hi = _n(st["min"], d), _n(st["max"], d)
    sep = "" if unit in ("%", "°C") else " "
    u = f"{sep}{unit}" if unit else ""
    return f"{lo}{u}" if lo == hi else f"{lo}–{hi}{u}"


def _hms(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


class DailyLog:
    def __init__(self, logs_dir: Path):
        self.dir = logs_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, ts: float) -> Path:
        return self.dir / (time.strftime("%Y-%m-%d", time.localtime(ts)) + ".txt")

    def _append(self, ts: float, text: str):
        p = self._path(ts)
        new = not p.exists()
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write(time.strftime("%Y-%m-%d", time.localtime(ts)) + "\n\n")
            f.write(text.rstrip("\n") + "\n\n")
            f.flush()

    def interval(self, closed: dict):
        iv, st = closed["interval"], closed["stats"]
        dur = int(iv["end"] - iv["start"])
        app = iv.get("fg_app") or "desconhecido"
        head = ("Sistema estavel." if iv["state"] == "estavel" else "Carga elevada.")
        lines = [f"[{_hms(iv['start'])}–{_hms(iv['end'])}] ({dur // 60} min {dur % 60} s)",
                 f"{head} Primeiro plano: {app}."]
        for key, label, unit, d in LINES:
            if key in st:
                lines.append(f"{label}: {_rng(st[key], d, unit)}")
        ac = iv.get("ac")
        if ac is not None:
            lines.append("Energia: " + ("tomada" if ac == "1" else "bateria"))
        lines.append(f"(corte: {iv['reason']})")
        self._append(iv["start"], "\n".join(lines))

    def event(self, ts: float, etype: str, severity: str, detail: dict):
        d = "; ".join(f"{k}={v}" for k, v in detail.items() if v is not None)
        self._append(ts, f"[{_hms(ts)}]\nEvento ({severity}): {etype}.\n{d}")
