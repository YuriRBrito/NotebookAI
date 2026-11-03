"""Testa a logica do coletor LHM com linhas sinteticas (formato da classe WMI Sensor).
Os nomes dos sensores sao plausiveis, NAO capturados do seu LHM: confirme com --list-sensors."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.collectors.lhm import LhmCollector, build_readings
from agent.readings import Unavailable
import agent.collectors.lhm as L

R = lambda i, n, t, v, p="/intelcpu/0": {"Identifier": i, "Name": n, "SensorType": t, "Value": v, "Min": v, "Max": v, "Parent": p}
ROWS = [R("/intelcpu/0/temperature/16", "CPU Package", "Temperature", 55.0),
        R("/intelcpu/0/temperature/0", "CPU Core #1", "Temperature", 52.0),
        R("/intelcpu/0/clock/1", "CPU Core #1", "Clock", 1200.0), R("/intelcpu/0/clock/2", "CPU Core #2", "Clock", 1800.0),
        R("/intelcpu/0/power/0", "CPU Package", "Power", 8.4), R("/intelcpu/0/load/0", "CPU Total", "Load", 12.0),
        R("/intelcpu/0/power/3", "CPU Platform", "Power", 1.1),
        R("/nvme/0/temperature/0", "Temperature", "Temperature", 41.0, "/nvme/0"),
        R("/nvme/0/level/1", "Percentage Used", "Level", 4.0, "/nvme/0"),
        R("/battery/0/energy/0", "Designed Capacity", "Energy", 58003.0, "/battery/0"),
        R("/gpu-intel/0/load/0", "D3D 3D", "Load", None, "/gpu-intel/0")]
k = {r.key: r for r in build_readings(ROWS, "LibreHardwareMonitor (WMI)")}
assert k["cpu.package_temp_c"].value == 55.0 and k["cpu.core_temp_c.1"].value == 52.0
assert k["cpu.package_power_w"].value == 8.4 and abs(k["cpu.clock_mhz"].value - 1500) < 1e-9
assert k["cpu.clock_mhz"].kind == "calculated" and k["ssd.life_remaining_pct"].value == 96.0
assert k["battery.lhm.design_mwh"].value == 58003.0 and k["ssd.temp_c"].value == 41.0
assert k["gpu.lhm.util_pct"].value is None and "null" in k["gpu.lhm.util_pct"].note     # null NAO vira zero
assert any(x.startswith("lhm.cpu.") and "cpu_platform" in x for x in k)                 # nao mapeado e preservado
assert "cpu.thermal_throttle" not in k                                                  # nao inventa sensor

c = LhmCollector()
def boom(): raise Unavailable("WMI: root/LibreHardwareMonitor: rc=1 Invalid namespace")
c._wmi = boom; c._http = lambda: (_ for _ in ()).throw(Unavailable("HTTP: recusado"))
L.lhm_running = lambda: False; L.winapi.require_windows = lambda: None
r = c.run()
assert r.status == "UNAVAILABLE" and "NAO esta em execucao" in r.reason and "Invalid namespace" in r.reason
c._wmi = lambda: ROWS; L.lhm_running = lambda: True
assert c.run().status == "OK"
print("OK test_lhm")
