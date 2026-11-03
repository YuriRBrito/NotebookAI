"""Visualizacao basica do que foi gravado: python ShowDb.py [--root NotebookAI_runtime] [--last 20]"""
import argparse
import sqlite3
import time

ap = argparse.ArgumentParser()
ap.add_argument("--root", default="NotebookAI_runtime")
ap.add_argument("--last", type=int, default=20)
a = ap.parse_args()
db = sqlite3.connect(f"file:{a.root}/Data/notebookai.db?mode=ro", uri=True)
hm = lambda ts: time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
print(f"Intervalos (ultimos {a.last}):")
KEYS = ("cpu.util_pct", "gpu.util_pct", "ram.used_gb", "cpu.package_temp_c", "gpu.temp_c")
for iid, s, e, state, app, reason in db.execute(
        "select i.id,i.start_ts,i.end_ts,i.state,a.friendly_name,i.close_reason from intervals i "
        "left join apps a on a.id=i.fg_app_id order by i.id desc limit ?", (a.last,)):
    parts = []
    for k in KEYS:
        r = db.execute("select min,max from interval_stats st join sensors se on se.id=st.sensor_id "
                       "where st.interval_id=? and se.key=?", (iid, k)).fetchone()
        if r:
            parts.append(f"{k.split('.')[0].upper()}{'T' if 'temp' in k else ''} {r[0]:.0f}-{r[1]:.0f}"
                         if 'ram' not in k else f"RAM {r[0]:.1f}-{r[1]:.1f}GB")
    n = db.execute("select count(*) from interval_stats where interval_id=?", (iid,)).fetchone()[0]
    print(f"  #{iid} {hm(s)} -> {hm(e) if e else '...'} | {state} | {app} | {' | '.join(parts)} | {n} sensores | corte: {reason}")
print("\nEventos:")
for ts, t, sev, d in db.execute("select ts,type,severity,detail_json from events order by id desc limit ?", (a.last,)):
    print(f"  {hm(ts)} {t} ({sev}) {d}")
print("\nBateria (snapshots):")
for r in db.execute("select ts,design_mwh,full_mwh,remaining_mwh,status,health_pct from battery_snapshots order by ts desc limit 10"):
    print(f"  {hm(r[0])} design={r[1]} full={r[2]} remaining={r[3]} {r[4]} health={r[5]:.1f}%" if r[5] is not None else f"  {r}")
print("\nSensores catalogados:", db.execute("select count(*) from sensors").fetchone()[0])
