"""Une motor de intervalos, detector de eventos, SQLite e TXT diario."""
import time
from pathlib import Path

from agent.events import EventDetector
from agent.intervals import IntervalEngine, Sample
from agent.store import Store
from agent.txtlog import DailyLog
from common.config import Config


class Recorder:
    def __init__(self, root: Path, cfg: Config | None = None, on_interval=None, on_event=None):
        self.cfg = cfg or Config()
        self.store = Store(root / "Data" / "notebookai.db")
        self.log = DailyLog(root / "Logs")
        self.engine = IntervalEngine(self.cfg)
        self.det = EventDetector(self.cfg)
        self._last_flush = time.time()
        self._app_paths: dict[str, str] = {}
        self.on_interval = on_interval
        self.on_event = on_event

    def register_app(self, name: str, exe_path: str | None):
        if name and exe_path:
            self._app_paths[name] = exe_path

    def _persist(self, closed_list: list[dict]):
        for c in closed_list:
            iv = c["interval"]
            name = iv.get("fg_app")
            if name:
                path = self._app_paths.get(name, name)
                iv["app_id"] = self.store.app_id(path, path.replace("/", "\\").split("\\")[-1], name)
            self.store.save_interval(iv, c["stats"], c["meta"])
            self.log.interval(c)
            if self.on_interval:
                self.on_interval(c)

    def _emit(self, ts, e):
        self.store.save_event(e.get("ts", ts), e["type"], e["severity"], e["detail"])
        self.log.event(e.get("ts", ts), e["type"], e["severity"], e["detail"])
        if self.on_event:
            self.on_event(e.get("ts", ts), e)

    def feed(self, s: Sample, extra_events: list[dict] | None = None):
        """extra_events: eventos externos (Event Log) com ts/type/severity/detail.
        Somente os de severidade 'critico' fecham o intervalo."""
        events = self.det.check(s) + list(extra_events or [])
        critical = [e for e in events if e["severity"] == "critico"]
        if critical:
            self._persist(self.engine.force_close(s, "evento:" + critical[0]["type"]))
        else:
            self._persist(self.engine.feed(s))
        for e in events:
            self._emit(s.ts, e)
        if events:
            self.store.commit()
            self._last_flush = s.ts
        elif s.ts - self._last_flush >= self.cfg.flush_secs:
            self.store.commit()
            self._last_flush = s.ts

    def shutdown(self, ts: float, reason: str):
        self._persist(self.engine.close_current(ts, reason))
        self._emit(ts, {"type": "encerramento", "severity": "info", "detail": {"motivo": reason}})
        self.store.checkpoint_and_close()
