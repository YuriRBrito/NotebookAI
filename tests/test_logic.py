"""Testes da LOGICA (rodam em qualquer SO). Eles NAO provam que o HWiNFO real tem estes rotulos:
os rotulos abaixo sao plausiveis, nao capturados de um HWiNFO de verdade.
Rodar: python tests/test_logic.py"""
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.apps import LoadThresholds, AppTracker, classify, friendly_name        # noqa: E402
from agent.collectors.battery import compute_battery                              # noqa: E402
from agent.collectors.eventlog import parse_events                                # noqa: E402
from agent.collectors.hwinfo import HEADER_FMT, HwinfoCollector, parse_buffer      # noqa: E402
from agent.collectors.system import aggregate_gpu_engines                         # noqa: E402
from agent.pipeline import Frame, to_sample                                       # noqa: E402
from agent.readings import Collector, Unavailable                                 # noqa: E402
from agent.recorder import Recorder                                               # noqa: E402

TYPE = {"temp": 1, "volt": 2, "fan": 3, "current": 4, "power": 5, "clock": 6, "usage": 7, "other": 8}


def _s(txt, n):
    return txt.encode("utf-8")[:n - 1].ljust(n, b"\x00")


def build_buffer(sensors, readings, sig=0x53695748, poll=1000):
    """sensors: [(id, nome)], readings: [(tipo, idx_sensor, id, rotulo, unidade, valor)]"""
    offS = 44
    szS, szR = 264, 316
    offR = offS + szS * len(sensors)
    b = bytearray(struct.pack(HEADER_FMT, sig, 2, 0, poll, offS, szS, len(sensors), offR, szR, len(readings)))
    for sid, name in sensors:
        b += struct.pack("<II", sid, 0) + _s(name, 128) + _s(name, 128)
    for t, si, rid, lab, unit, val in readings:
        b += struct.pack("<III", TYPE[t], si, rid) + _s(lab, 128) + _s(lab, 128) + _s(unit, 16)
        b += struct.pack("<4d", val, val, val, val)
    return bytes(b)


SENSORS = [(1, "CPU [#0]: Intel Core i5-1235U: DTS"), (2, "CPU [#0]: Intel Core i5-1235U"),
           (3, "GPU [#0]: Intel Iris Xe Graphics"), (4, "Drive: Fabricante SSD NVMe"),
           (5, "Battery: Dell"), (6, "Memory Timings"), (7, "Network: Wi-Fi"), (8, "CPU [#0]: Performance Limit Reasons")]
READS = [
    ("temp", 0, 1, "CPU Package", "°C", 55.0), ("temp", 0, 2, "Core 0", "°C", 52.0), ("temp", 0, 3, "Core 1", "°C", 58.0),
    ("temp", 0, 4, "Core Max", "°C", 58.0),
    ("power", 1, 5, "CPU Package Power", "W", 8.4), ("clock", 1, 6, "Core 0 Clock", "MHz", 1200.0),
    ("clock", 1, 7, "Core 1 Clock", "MHz", 1800.0), ("usage", 1, 8, "Total CPU Usage", "%", 12.0),
    ("other", 7, 9, "Package/Ring Thermal Throttling", "Yes/No", 0.0), ("other", 7, 10, "Core 0 Thermal Throttling", "Yes/No", 1.0),
    ("other", 7, 11, "IA: Power Limit Exceeded", "Yes/No", 0.0),
    ("temp", 2, 12, "GPU Temperature", "°C", 43.0), ("clock", 2, 13, "GPU Clock", "MHz", 210.0),
    ("temp", 3, 14, "Drive Temperature", "°C", 41.0), ("other", 3, 15, "Drive Remaining Life", "%", 96.0),
    ("other", 4, 16, "Designed Capacity", "Wh", 58.003), ("other", 4, 17, "Full Charged Capacity", "Wh", 46.8),
    ("volt", 4, 18, "Battery Voltage", "V", 12.1),
    ("clock", 5, 19, "Memory Clock", "MHz", 2400.0),
    ("other", 6, 20, "Total Download", "MB", 123.0),
]


def test_hwinfo_parser_and_mapping():
    buf = build_buffer(SENSORS, READS)
    h, s, r = parse_buffer(buf)
    assert len(s) == 8 and len(r) == len(READS)
    res = HwinfoCollector().read_buffer(buf)
    keys = {x.key: x for x in res.readings}
    assert keys["cpu.package_temp_c"].value == 55.0
    assert keys["cpu.core_temp_c.0"].value == 52.0 and keys["cpu.core_temp_c.1"].value == 58.0
    assert keys["cpu.package_power_w"].value == 8.4
    assert keys["cpu.thermal_throttle"].value == 1.0          # max de varios flags (1 core em throttling)
    assert keys["cpu.power_limit"].value == 0.0
    assert abs(keys["cpu.clock_mhz"].value - 1500.0) < 1e-6 and keys["cpu.clock_mhz"].kind == "calculated"
    assert keys["gpu.temp_c"].value == 43.0 and keys["ssd.temp_c"].value == 41.0
    assert abs(keys["battery.hwinfo.design_mwh"].value - 58003.0) < 1e-6      # Wh -> mWh
    # nao mapeado e preservado, nunca descartado:
    assert any(k.startswith("hwinfo.cpu.") and "core_max" in k for k in keys), sorted(keys)
    assert any(k.startswith("hwinfo.network.") for k in keys)
    assert all(x.source.startswith("HWiNFO") for x in res.readings)


def test_hwinfo_dead_and_bad_layout():
    try:
        parse_buffer(build_buffer(SENSORS, READS, sig=0x44414544))
        raise AssertionError("deveria sinalizar DEAD")
    except Unavailable:
        pass
    bad = bytearray(build_buffer(SENSORS, READS))
    bad[0:4] = b"XXXX"
    try:
        parse_buffer(bytes(bad))
        raise AssertionError("assinatura invalida deveria falhar")
    except ValueError:
        pass


def test_battery_dynamic_not_fixed():
    info = {"design_mwh": 58003, "full_mwh": 46800, "cycles": 0, "relative_capacity": False}
    st = {"remaining_mwh": 38376, "voltage_mv": 12100, "rate_mw": 15000, "charging": True,
          "discharging": False, "online": True}
    sps = {"ac": True, "battery_pct": 82, "life_secs": None}
    R, d = compute_battery(info, st, sps, None, None, 58003)
    k = {r.key: r for r in R}
    assert abs(k["battery.health_pct"].value - 80.69) < 0.01 and abs(k["battery.wear_pct"].value - 19.31) < 0.01
    assert d["status"] == "Charging" and d["reference_check"]["diff_mwh"] == 0
    assert k["battery.estimated_full_mah"].value is None and "NOMINAL" in k["battery.estimated_full_mah"].note
    assert k["battery.cycles"].note                       # avisa que 0 pode ser "nao informado"
    # firmware com OUTRO valor: o valor real prevalece e a discrepancia e registrada
    info2 = dict(info, design_mwh=54000, full_mwh=43000)
    R2, d2 = compute_battery(info2, st, sps, None, None, 58003)
    assert {r.key: r for r in R2}["battery.design_mwh"].value == 54000
    assert d2["reference_check"]["diff_mwh"] == -4003
    # mAh so com tensao nominal informada, rotulado "estimated"
    R3, _ = compute_battery(info, st, sps, None, 11400, None)
    m = {r.key: r for r in R3}["battery.estimated_full_mah"]
    assert m.kind == "estimated" and abs(m.value - 46800 / 11.4) < 1e-6


def test_open_vs_active():
    t = LoadThresholds()
    assert classify(1.0, 0.0, False, t) == ("OPEN", "LOW")           # minimizado/parado
    assert classify(62.0, 91.0, True, t) == ("ACTIVE", "HIGH")      # primeiro plano e consumindo
    assert classify(1.0, 0.0, True, t) == ("OPEN", "LOW")           # primeiro plano mas ocioso
    assert classify(15.0, 0.0, False, t)[0] == "ACTIVE"             # em 2o plano consumindo
    tr = AppTracker()
    g = [{"key": "a", "pids": [10], "cpu_pct": 62.0, "exe": "javaw.exe"}]
    out = tr.update(g, 10, {10: 91.0})
    assert out[0]["state"] == "ACTIVE" and out[0]["load"] == "HIGH" and out[0]["foreground"]


def test_friendly_names_generic():
    rules = [(r"^javaw?\.exe$", r"^Minecraft", "Minecraft Java")]
    assert friendly_name("C:/x/javaw.exe", "javaw.exe", "Minecraft 1.20.1", "OpenJDK", rules) == "Minecraft Java"
    assert friendly_name("C:/x/javaw.exe", "javaw.exe", "Outro App - v1", "OpenJDK", rules) == "Outro App"
    assert friendly_name("C:/c/chrome.exe", "chrome.exe", "Aba", "Google Chrome", rules) == "Google Chrome"
    assert friendly_name("C:/w/explorer.exe", "explorer.exe", "Pasta", None, rules) == "explorer"


def test_gpu_engine_aggregation():
    items = {"pid_100_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D": 40.0,
             "pid_200_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D": 25.0,
             "pid_200_luid_0x00000000_0x0000AAAA_phys_0_eng_1_engtype_Copy": 5.0}
    util, by_type, per_pid = aggregate_gpu_engines(items)
    assert util == 65.0 and by_type["Copy"] == 5.0 and per_pid[100] == 40.0 and per_pid[200] == 25.0


def test_eventlog_parse():
    xml = ('<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
           '<Provider Name="Microsoft-Windows-WHEA-Logger"/><EventID>17</EventID><EventRecordID>99</EventRecordID>'
           '<TimeCreated SystemTime="2026-11-02T05:31:00.1234567Z"/></System>'
           '<EventData><Data Name="ErrorSource">3</Data></EventData></Event>')
    ev = parse_events(xml)[0]
    assert ev["type"] == "whea" and ev["severity"] == "critico" and ev["data"]["ErrorSource"] == "3"


def test_failure_isolation():
    class Boom(Collector):
        name = "boom"

        def collect(self):
            raise RuntimeError("falha qualquer")

    class Gone(Collector):
        name = "gone"

        def collect(self):
            raise Unavailable("fonte ausente")

    a, b = Boom().run(), Gone().run()
    assert a.status == "FAILED" and "RuntimeError" in a.reason
    assert b.status == "UNAVAILABLE" and b.reason == "fonte ausente"


def test_real_shaped_frame_into_engine():
    """Frame com leituras no formato real -> Sample -> Recorder (SQLite+TXT) sem excecoes."""
    from agent.readings import Reading
    tmp = Path(tempfile.mkdtemp())
    rec = Recorder(tmp)
    t0 = 1_800_000_000.0
    for i in range(60):
        f = Frame(ts=t0 + i * 2)
        for k, v, u in (("cpu.util_pct", 10 + (i % 3), "%"), ("ram.used_gb", 7.5, "GB"),
                        ("battery.ac", 1.0, ""), ("battery.pct", 82.0, "%")):
            f.readings[k] = Reading(k, v, u, "teste")
        f.foreground = {"friendly": "Chrome", "path": "C:/chrome.exe", "pid": 1}
        rec.register_app("Chrome", "C:/chrome.exe")
        rec.feed(to_sample(f))
    rec.shutdown(t0 + 120, "teste")
    import sqlite3
    db = sqlite3.connect(tmp / "Data" / "notebookai.db")
    assert db.execute("select count(*) from intervals").fetchone()[0] == 1       # 60 amostras -> 1 intervalo
    assert list((tmp / "Logs").glob("*.txt"))


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK    {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                traceback.print_exc()
    sys.exit(1 if fails else 0)
