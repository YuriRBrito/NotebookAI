"""Windows Event Log via wevtutil (sem dependencias). Le o log System; nao usa o log Security."""
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from agent import winapi
from agent.readings import Collector, CollectorResult, Unavailable

NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}

# (provider, event_id|None) -> (tipo, severidade, descricao)
CLASSES = [
    ("Microsoft-Windows-WHEA-Logger", None, "whea", "critico"),
    ("Microsoft-Windows-Kernel-Power", 41, "desligamento_inesperado_kernel_power_41", "critico"),
    ("EventLog", 6008, "desligamento_anterior_inesperado", "critico"),
    ("Microsoft-Windows-WER-SystemErrorReporting", 1001, "bsod_bugcheck", "critico"),
    ("Microsoft-Windows-Kernel-Power", 42, "sleep", "info"),
    ("Microsoft-Windows-Kernel-Power", 107, "wake", "info"),
    ("Microsoft-Windows-Power-Troubleshooter", 1, "wake", "info"),
    ("User32", 1074, "shutdown_ou_restart_solicitado", "info"),
    ("Microsoft-Windows-Kernel-General", 12, "inicio_do_sistema", "info"),
    ("Microsoft-Windows-Kernel-General", 13, "encerramento_do_sistema", "info"),
]
PROVIDERS = sorted({c[0] for c in CLASSES})


def classify(provider: str, eid: int):
    for p, i, t, sev in CLASSES:
        if p == provider and (i is None or i == eid):
            return t, sev
    return None


def parse_events(xml_text: str) -> list[dict]:
    out = []
    root = ET.fromstring("<root>" + xml_text + "</root>")
    for ev in root.findall("e:Event", NS):
        sysn = ev.find("e:System", NS)
        prov = sysn.find("e:Provider", NS).get("Name")
        eid = int(sysn.find("e:EventID", NS).text)
        rec = int(sysn.find("e:EventRecordID", NS).text)
        t = sysn.find("e:TimeCreated", NS).get("SystemTime")
        t = t.rstrip("Z")
        if "." in t:
            head, frac = t.split(".")
            t = f"{head}.{frac[:6]}"
        ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()
        data = [(d.get("Name") or "", (d.text or "").strip()) for d in ev.findall("e:EventData/e:Data", NS)]
        c = classify(prov, eid)
        out.append({"record": rec, "ts": ts, "provider": prov, "id": eid,
                    "type": c[0] if c else None, "severity": c[1] if c else None,
                    "data": {k or f"d{i}": v for i, (k, v) in enumerate(data[:6]) if v}})
    return out


def query(window_ms: int, max_events: int = 100) -> list[dict]:
    winapi.require_windows()
    provs = " or ".join(f"Provider[@Name='{p}']" for p in PROVIDERS)
    xpath = f"*[System[({provs}) and TimeCreated[timediff(@SystemTime) <= {int(window_ms)}]]]"
    try:
        cp = subprocess.run(["wevtutil", "qe", "System", f"/q:{xpath}", f"/c:{max_events}",
                             "/rd:true", "/f:xml"], capture_output=True, text=True, timeout=30,
                            creationflags=0x08000000)
    except FileNotFoundError:
        raise Unavailable("wevtutil.exe nao encontrado")
    if cp.returncode == 5:
        raise Unavailable("acesso negado ao log System (erro 5)")
    if cp.returncode != 0:
        raise Unavailable(f"wevtutil retornou {cp.returncode}: {cp.stderr.strip()[:200]}")
    return parse_events(cp.stdout) if cp.stdout.strip() else []


class EventLogCollector(Collector):
    name = "Event Log"
    admin_expected = "nao para o log System (Security exigiria; nao usado); a verificar"

    def __init__(self):
        self.seen: set[int] = set()
        self.last_poll: float | None = None

    def collect(self, window_ms: int | None = None) -> CollectorResult:
        import time
        now = time.time()
        window_ms = window_ms or getattr(self, "override_window_ms", None)
        if window_ms is None:
            window_ms = 60_000 if self.last_poll is None else int((now - self.last_poll) * 1000) + 5_000
        events = query(window_ms)
        self.last_poll = now
        new = [e for e in events if e["record"] not in self.seen and e["type"]]
        self.seen.update(e["record"] for e in events)
        return CollectorResult(self.name, data={"events": new, "scanned": len(events)})
