"""Persistencia SQLite (modo WAL). Somente o Agent escreve."""
import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sensors(
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, label TEXT, unit TEXT,
    source TEXT, first_seen REAL);
CREATE TABLE IF NOT EXISTS apps(
    id INTEGER PRIMARY KEY, exe_path TEXT UNIQUE NOT NULL, exe_name TEXT,
    friendly_name TEXT);
CREATE TABLE IF NOT EXISTS intervals(
    id INTEGER PRIMARY KEY, start_ts REAL NOT NULL, end_ts REAL,
    state TEXT, fg_app_id INTEGER REFERENCES apps(id), ac INTEGER,
    close_reason TEXT);
CREATE TABLE IF NOT EXISTS interval_stats(
    interval_id INTEGER NOT NULL REFERENCES intervals(id),
    sensor_id INTEGER NOT NULL REFERENCES sensors(id),
    min REAL, max REAL, avg REAL, n INTEGER,
    PRIMARY KEY(interval_id, sensor_id));
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, type TEXT NOT NULL,
    severity TEXT NOT NULL, detail_json TEXT, interval_id INTEGER);
CREATE TABLE IF NOT EXISTS app_sessions(
    id INTEGER PRIMARY KEY, app_id INTEGER REFERENCES apps(id), pid INTEGER,
    start_ts REAL, end_ts REAL, foreground_secs REAL, active_secs REAL,
    cpu_avg REAL, cpu_max REAL, gpu_avg REAL, gpu_max REAL,
    ram_avg_mb REAL, ram_max_mb REAL, temp_cpu_max REAL, power_avg_w REAL,
    fps_avg REAL, fps_1low REAL, fps_01low REAL, load_class TEXT);
CREATE TABLE IF NOT EXISTS battery_snapshots(
    ts REAL, design_mwh REAL, full_mwh REAL, remaining_mwh REAL,
    voltage_mv REAL, rate_mw REAL, status TEXT, ac INTEGER, cycles INTEGER,
    health_pct REAL);
CREATE TABLE IF NOT EXISTS system_boots(
    id INTEGER PRIMARY KEY, boot_ts REAL, shutdown_ts REAL,
    shutdown_reason TEXT, clean INTEGER);
CREATE INDEX IF NOT EXISTS ix_intervals_start ON intervals(start_ts);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts, type);
CREATE INDEX IF NOT EXISTS ix_stats_sensor ON interval_stats(sensor_id, interval_id);
CREATE INDEX IF NOT EXISTS ix_sessions_app ON app_sessions(app_id, start_ts);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),))
        self.db.commit()
        self._sensor_ids: dict[str, int] = {}
        self._app_ids: dict[str, int] = {}

    def sensor_id(self, key, label=None, unit=None, source=None, ts=0.0) -> int:
        sid = self._sensor_ids.get(key)
        if sid is None:
            self.db.execute(
                "INSERT OR IGNORE INTO sensors(key,label,unit,source,first_seen) VALUES(?,?,?,?,?)",
                (key, label or key, unit, source, ts))
            sid = self.db.execute("SELECT id FROM sensors WHERE key=?", (key,)).fetchone()[0]
            self._sensor_ids[key] = sid
        return sid

    def app_id(self, exe_path: str, exe_name: str, friendly: str) -> int:
        aid = self._app_ids.get(exe_path)
        if aid is None:
            self.db.execute(
                "INSERT OR IGNORE INTO apps(exe_path,exe_name,friendly_name) VALUES(?,?,?)",
                (exe_path, exe_name, friendly))
            aid = self.db.execute("SELECT id FROM apps WHERE exe_path=?", (exe_path,)).fetchone()[0]
            self._app_ids[exe_path] = aid
        return aid

    def save_interval(self, iv: dict, stats: dict[str, dict], meta: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO intervals(start_ts,end_ts,state,fg_app_id,ac,close_reason) VALUES(?,?,?,?,?,?)",
            (iv["start"], iv["end"], iv["state"], iv.get("app_id"), iv.get("ac"), iv["reason"]))
        iid = cur.lastrowid
        for key, s in stats.items():
            m = meta.get(key, {})
            sid = self.sensor_id(key, m.get("label"), m.get("unit"), m.get("source"), iv["start"])
            self.db.execute(
                "INSERT INTO interval_stats VALUES(?,?,?,?,?,?)",
                (iid, sid, s["min"], s["max"], s["sum"] / s["n"], s["n"]))
        return iid

    def save_event(self, ts, etype, severity, detail: dict, interval_id=None):
        self.db.execute(
            "INSERT INTO events(ts,type,severity,detail_json,interval_id) VALUES(?,?,?,?,?)",
            (ts, etype, severity, json.dumps(detail, ensure_ascii=False), interval_id))

    def meta_get(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(self, key, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def save_battery_snapshot(self, ts, v: dict):
        self.db.execute(
            "INSERT INTO battery_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, v.get("design_mwh"), v.get("full_mwh"), v.get("remaining_mwh"), v.get("voltage_mv"),
             v.get("rate_mw"), v.get("status"), v.get("ac"), v.get("cycles"), v.get("health_pct")))

    def commit(self):
        self.db.commit()

    def checkpoint_and_close(self):
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()
