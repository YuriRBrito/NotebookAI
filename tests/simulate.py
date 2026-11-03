"""Simula ~7 h de uso a 2 s por amostra e confere que o banco guarda poucos intervalos."""
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.intervals import Sample          # noqa: E402
from agent.recorder import Recorder         # noqa: E402

random.seed(7)
root = Path("/tmp/notebookai_sim")
shutil.rmtree(root, ignore_errors=True)
rec = Recorder(root)
rec.register_app("Minecraft Java", r"C:\Program Files\Java\bin\javaw.exe")
rec.register_app("Chrome", r"C:\Program Files\Google\Chrome\chrome.exe")

t0 = time.mktime((2026, 11, 2, 16, 50, 31, 0, 0, -1))


def phase(ts_from, ts_to, **kw):
    return (ts_from, ts_to, kw)


# (inicio_s, fim_s, parametros) relativos a t0
PHASES = [
    (0, 3 * 3600 + 41 * 60 + 31, dict(app="Chrome", cpu=(4, 18), gpu=(0, 5), ram=(7.4, 7.6), temp=(42, 49), ac="1")),
    (3 * 3600 + 41 * 60 + 32, 3 * 3600 + 44 * 60 + 33,
     dict(app="Minecraft Java", cpu=(45, 72), gpu=(60, 91), ram=(12.0, 13.8), temp=(70, 84), ac="1", fps=(58, 62))),
    (3 * 3600 + 44 * 60 + 34, 4 * 3600 + 20 * 60,
     dict(app="Chrome", cpu=(4, 18), gpu=(0, 5), ram=(7.4, 7.6), temp=(42, 49), ac="1")),
    (4 * 3600 + 20 * 60 + 1, 4 * 3600 + 30 * 60,
     dict(app="Chrome", cpu=(4, 18), gpu=(0, 5), ram=(7.4, 7.6), temp=(42, 49), ac="0")),
]
THROTTLE_AT = 3 * 3600 + 43 * 60

n = 0
for a, b, p in PHASES:
    ts = t0 + a
    while ts <= t0 + b:
        rel = ts - t0
        temp = random.uniform(*p["temp"])
        thr = 0
        if rel >= THROTTLE_AT and rel < THROTTLE_AT + 30:
            temp, thr = 96.0, 1
        vals = {
            "cpu.util_pct": random.uniform(*p["cpu"]),
            "gpu.util_pct": random.uniform(*p["gpu"]),
            "ram.used_gb": random.uniform(*p["ram"]),
            "ram.used_pct": random.uniform(*p["ram"]) / 16 * 100,
            "cpu.package_temp_c": temp,
            "cpu.thermal_throttle": thr,
            "battery.pct": 82.0,
        }
        if "fps" in p:
            vals["fps"] = random.uniform(*p["fps"])
        meta = {"cpu.package_temp_c": {"label": "CPU Package", "unit": "C", "source": "HWiNFO"}}
        rec.feed(Sample(ts, vals, {"state.fg_app": p["app"], "state.ac": p["ac"]}, meta))
        n += 1
        ts += 2.0
rec.shutdown(ts, "encerramento_simulado")

db = sqlite3.connect(root / "Data" / "notebookai.db")
print("amostras internas:", n)
print("intervalos:", db.execute("select count(*) from intervals").fetchone()[0])
print("eventos:", db.execute("select count(*) from events").fetchone()[0])
print("linhas interval_stats:", db.execute("select count(*) from interval_stats").fetchone()[0])
for r in db.execute("select time(start_ts,'unixepoch','localtime'), time(end_ts,'unixepoch','localtime'), state, close_reason from intervals"):
    print(r)
for r in db.execute("select time(ts,'unixepoch','localtime'), type, severity from events"):
    print(r)
