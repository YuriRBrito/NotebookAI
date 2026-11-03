"""NotebookAI Agent (modo desenvolvimento): --diagnostic, --list-sensors, --monitor, --apps."""
import argparse
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from agent import winapi
from agent.intervals import Sample
from agent.normalize import categorize
from agent.pipeline import Frame, Pipeline, to_sample
from agent.recorder import Recorder
from agent.readings import Reading
from common.config import Config

VERSION = "0.1.1-dev"


# ----------------------------------------------------------------------
# formatacao
# ----------------------------------------------------------------------
def num(v: float, d: int = 1) -> str:
    s = f"{v:,.{d}f}"                      # 58,003.0
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def fmt_reading(r: Reading) -> str:
    u = r.unit
    if u.strip().lower() == "yes/no":
        return "YES" if r.value else "NO"
    d = {"%": 0, "°C": 0, "C": 0, "W": 1, "GB": 2, "MB": 0, "MHz": 0, "MT/s": 0, "mWh": 0, "mV": 0,
         "mW": 0, "mA": 0, "MB/s": 1, "s": 0, "": 0}.get(u, 2)
    return f"{num(r.value, d)}{'' if u in ('%', '°C') else ' '}{u}".strip()


class Out:
    def __init__(self):
        self.lines: list[str] = []

    def p(self, s: str = ""):
        print(s)
        self.lines.append(s)


def why_missing(f: Frame, src: str) -> str:
    res = f.results.get(src)
    if res is None:
        return f"{src}: coletor nao executado"
    if res.status != "OK":
        return f"{src} {res.status}: {res.reason}"
    return f"nao encontrado entre os sensores/contadores detectados por {src}"


def line(o: Out, f: Frame, label: str, key: str, src: str, flag: bool = False):
    r = f.readings.get(key)
    if r is not None and r.value is not None:
        v = ("YES" if r.value else "NO") if flag else fmt_reading(r)
        extra = f", {r.kind}" if r.kind != "measured" else ""
        note = f"  ({r.note})" if r.note else ""
        o.p(f"  {label}: {v}   [fonte: {r.source}{extra}]{note}")
    elif r is not None:
        o.p(f"  {label}: N/D — {r.note or 'sensor nao disponivel'}")
    else:
        o.p(f"  {label}: N/D — sensor nao disponivel ({why_missing(f, src)})")


def multi(o: Out, f: Frame, label: str, prefix: str, src: str):
    rs = sorted((r for k, r in f.readings.items() if re.fullmatch(prefix + r"\.\w+", k) and r.value is not None),
                key=lambda r: r.key)
    if not rs:
        o.p(f"  {label}: N/D — sensor nao disponivel ({why_missing(f, src)})")
        return
    vals = [r.value for r in rs]
    unit = rs[0].unit
    o.p(f"  {label}: {num(min(vals), 0)}–{num(max(vals), 0)} {unit}  ({len(rs)} sensores)   [fonte: {rs[0].source}]")
    o.p("    " + "  ".join(f"{r.key.rsplit('.', 1)[1]}={num(r.value, 0)}" for r in rs))


def others(o: Out, f: Frame, cat: str):
    ks = sorted(k for k in f.readings if k.startswith(f"lhm.{cat}."))
    if not ks:
        o.p(f"  Outros sensores: nenhum sensor LibreHardwareMonitor nao mapeado nesta categoria")
        return
    o.p(f"  Outros sensores (LibreHardwareMonitor, nao mapeados para chave canonica): {len(ks)}")
    for k in ks[:12]:
        r = f.readings[k]
        o.p(f"    {r.label}: {fmt_reading(r)}   [{k}]")
    if len(ks) > 12:
        o.p(f"    ... +{len(ks) - 12} (use --list-sensors)")


# ----------------------------------------------------------------------
# secoes do diagnostico
# ----------------------------------------------------------------------
def sec_cpu(o, f):
    o.p("\nCPU")
    cpu = f.results.get("Windows CPU")
    name = cpu.data.get("cpu_name") if cpu and cpu.status == "OK" else None
    o.p(f"  CPU: {name or 'N/D — ' + why_missing(f, 'Windows CPU')}   [fonte: Registro do Windows]" if name
        else f"  CPU: N/D — {why_missing(f, 'Windows CPU')}")
    line(o, f, "Utilizacao", "cpu.util_pct", "Windows CPU")
    line(o, f, "Package Temperature", "cpu.package_temp_c", "LibreHardwareMonitor")
    multi(o, f, "Core Temperatures", r"cpu\.core_temp_c", "LibreHardwareMonitor")
    line(o, f, "Current Clock (media dos nucleos)", "cpu.clock_mhz", "LibreHardwareMonitor")
    line(o, f, "Clock - Windows PDH (derivado)", "cpu.windows.freq_mhz", "Windows CPU")
    line(o, f, "Clock - psutil", "cpu.psutil.freq_mhz", "Windows CPU")
    line(o, f, "Effective Clock", "cpu.effective_clock_mhz", "LibreHardwareMonitor")
    line(o, f, "Package Power", "cpu.package_power_w", "LibreHardwareMonitor")
    line(o, f, "Core Power", "cpu.core_power_w", "LibreHardwareMonitor")
    line(o, f, "PL1", "cpu.pl1_w", "LibreHardwareMonitor")
    line(o, f, "PL2", "cpu.pl2_w", "LibreHardwareMonitor")
    line(o, f, "Thermal Throttling", "cpu.thermal_throttle", "LibreHardwareMonitor", flag=True)
    line(o, f, "Power Limit", "cpu.power_limit", "LibreHardwareMonitor", flag=True)
    line(o, f, "Current/EDP Limit", "cpu.edp_limit", "LibreHardwareMonitor", flag=True)
    others(o, f, "cpu")


def sec_gpu(o, f):
    o.p("\nGPU")
    g = f.results.get("Windows GPU")
    ads = g.data.get("adapters") if g and g.status == "OK" else None
    o.p("  GPU: " + (", ".join(a.get("Name", "?") for a in ads) + "   [fonte: Windows CIM]" if ads
                     else "N/D — " + why_missing(f, "Windows GPU") + " / CIM sem adaptadores"))
    line(o, f, "GPU Utilization (Windows PDH)", "gpu.util_pct", "Windows GPU")
    line(o, f, "GPU Utilization (LibreHardwareMonitor)", "gpu.lhm.util_pct", "LibreHardwareMonitor")
    line(o, f, "GPU Clock", "gpu.clock_mhz", "LibreHardwareMonitor")
    line(o, f, "GPU Temperature", "gpu.temp_c", "LibreHardwareMonitor")
    line(o, f, "GPU Hot Spot", "gpu.hotspot_c", "LibreHardwareMonitor")
    line(o, f, "GPU Power", "gpu.power_w", "LibreHardwareMonitor")
    line(o, f, "Shared Memory em uso", "gpu.shared_mem_mb", "Windows GPU")
    line(o, f, "Dedicated Memory em uso", "gpu.dedicated_mem_mb", "Windows GPU")
    line(o, f, "Memoria (LibreHardwareMonitor)", "gpu.lhm.mem_used", "LibreHardwareMonitor")
    line(o, f, "Throttling", "gpu.thermal_throttle", "LibreHardwareMonitor", flag=True)
    others(o, f, "gpu")


def sec_ram(o, f):
    o.p("\nRAM")
    for lab, k in (("Total", "ram.total_gb"), ("Used", "ram.used_gb"), ("Available", "ram.available_gb"),
                   ("Free", "ram.free_gb"), ("Committed", "ram.commit_gb"), ("Commit Limit", "ram.commit_limit_gb"),
                   ("Usage", "ram.used_pct"), ("Commit %", "ram.commit_pct"),
                   ("Pagefile (uso)", "ram.pagefile_usage_pct"), ("Pagefile (tamanho)", "ram.pagefile_size_mb"),
                   ("Pagefile (usado)", "ram.pagefile_used_mb"), ("Frequency", "ram.freq_mts")):
        line(o, f, lab, k, "Windows RAM")
    line(o, f, "Memory Clock (LibreHardwareMonitor)", "ram.lhm.memory_clock_mhz", "LibreHardwareMonitor")


def sec_ssd(o, f):
    o.p("\nSSD")
    s = f.results.get("Windows SSD")
    disks = s.data.get("disks") if s and s.status == "OK" else None
    if disks:
        for d in disks:
            o.p(f"  Modelo: {d.get('FriendlyName')}  ({d.get('MediaType')}, {d.get('BusType')})   [fonte: Get-PhysicalDisk]")
            o.p(f"  Health (Windows): {d.get('HealthStatus')} / {d.get('OperationalStatus')}   [fonte: Get-PhysicalDisk]")
    else:
        o.p(f"  Modelo: N/D — {why_missing(f, 'Windows SSD')} (Get-PhysicalDisk sem retorno)")
    line(o, f, "Capacidade", "ssd.capacity_gb", "Windows SSD")
    line(o, f, "Temperatura", "ssd.temp_c", "LibreHardwareMonitor")
    line(o, f, "Leitura", "ssd.read_mbps", "Windows SSD")
    line(o, f, "Escrita", "ssd.write_mbps", "Windows SSD")
    line(o, f, "Atividade", "ssd.activity_pct", "Windows SSD")
    line(o, f, "Vida restante (LibreHardwareMonitor)", "ssd.life_remaining_pct", "LibreHardwareMonitor")
    others(o, f, "ssd")


def sec_battery(o, f, cfg):
    o.p("\nBattery")
    b = f.results.get("Battery API")
    if b is None or b.status != "OK":
        o.p(f"  Battery API: {b.status if b else 'nao executado'} — {b.reason if b else ''}")
        o.p("  (nenhum valor de bateria sera inventado)")
        return
    d = b.data
    o.p(f"  Dispositivo: {d.get('name') or 'N/D'} | Fabricante: {d.get('manufacturer') or 'N/D'} | "
        f"Quimica: {d.get('chemistry')} | Baterias: {d.get('batteries_found')}")
    if d.get("warning"):
        o.p(f"  ATENCAO: {d['warning']}")
    line(o, f, "Design Capacity", "battery.design_mwh", "Battery API")
    line(o, f, "Full Charge Capacity", "battery.full_mwh", "Battery API")
    line(o, f, "Current Capacity", "battery.remaining_mwh", "Battery API")
    o.p("    (a Battery API tem um unico campo; 'Current' e 'Remaining' sao o mesmo valor)")
    line(o, f, "Charge level (Windows)", "battery.pct", "Battery API")
    line(o, f, "Charge calculado (Remaining/Full)", "battery.charge_calc_pct", "Battery API")
    line(o, f, "Health", "battery.health_pct", "Battery API")
    line(o, f, "Wear", "battery.wear_pct", "Battery API")
    line(o, f, "Voltage", "battery.voltage_mv", "Battery API")
    line(o, f, "Current", "battery.current_ma", "Battery API")
    line(o, f, "Rate", "battery.rate_mw", "Battery API")
    line(o, f, "Power", "battery.power_w", "Battery API")
    line(o, f, "Cycles", "battery.cycles", "Battery API")
    o.p(f"  Status: {d.get('status')}   [fonte: Windows Battery IOCTL]")
    line(o, f, "AC (Windows)", "battery.ac", "Battery API", flag=True)
    o.p(f"  AC (IOCTL): {'Connected' if d.get('ac_ioctl_online') else 'Disconnected'}   [fonte: Windows Battery IOCTL]")
    line(o, f, "Estimated Remaining (IOCTL)", "battery.est_remaining_s_ioctl", "Battery API")
    line(o, f, "Estimated Remaining (Windows)", "battery.est_remaining_s_windows", "Battery API")
    line(o, f, "Estimated Remaining (linear)", "battery.est_remaining_s_calc", "Battery API")
    line(o, f, "Estimated Time to Full (linear)", "battery.est_to_full_s_calc", "Battery API")
    r = f.readings.get("battery.estimated_full_mah")
    if r is not None and r.value is None:
        o.p(f"  Capacity: N/D — {r.note}")
    elif r is not None:
        o.p(f"  Estimated mAh: {fmt_reading(r)}   [{r.note}]")
    hw = sorted(k for k in f.readings if k.startswith("battery.lhm."))
    if hw:
        o.p("  Validacao cruzada com LibreHardwareMonitor:")
        for k in hw:
            o.p(f"    {f.readings[k].label}: {fmt_reading(f.readings[k])}   [{k}]")
    else:
        o.p("  Validacao cruzada com LibreHardwareMonitor: N/D — " + why_missing(f, "LibreHardwareMonitor"))
    rc = d.get("reference_check")
    o.p("\n  Validacao dos 58.003 mWh")
    if rc:
        o.p(f"    Design Capacity detectada: {num(rc['detected_mwh'], 0)} mWh")
        o.p(f"    Valor de referencia informado pelo usuario: {num(rc['reference_mwh'], 0)} mWh")
        o.p(f"    Diferenca: {num(rc['diff_mwh'], 0)} mWh ({num(rc['diff_pct'], 2)}%)  "
            + ("-> coerente" if abs(rc["diff_pct"]) < 1 else "-> DISCREPANCIA (registrada; o valor do firmware prevalece)"))


def sec_foreground(o, f):
    o.p("\nForeground")
    fg = f.foreground
    if not fg:
        o.p(f"  N/D — {why_missing(f, 'Foreground')}" if f.results.get("Foreground") and f.results["Foreground"].status != "OK"
            else "  Nenhuma janela em primeiro plano")
        return
    o.p(f"  Foreground: {fg['friendly']}")
    o.p(f"  Process: {fg['exe']}   PID: {fg['pid']}   Minimizada: {'sim' if fg['minimized'] else 'nao'}")
    o.p(f"  Path: {fg['path'] or 'N/D — acesso negado ao caminho'}")
    o.p(f"  Titulo da janela: {fg['title']!r}   FileDescription: {fg['description']!r}")
    if fg["notes"]:
        o.p(f"  Notas: {'; '.join(fg['notes'])}")
    o.p("  [fonte: Windows user32 + psutil]")


def apps_table(o, f, top=10):
    pr = f.results.get("Processes")
    if not pr or pr.status != "OK":
        o.p(f"  N/D — {why_missing(f, 'Processes')}")
        return
    apps = sorted(f.apps, key=lambda a: a["cpu_smooth"] + a["gpu_smooth"], reverse=True)[:top]
    o.p(f"  {'Aplicativo':<28}{'PID':>7} {'CPU':>6} {'GPU':>6} {'RAM(WS)':>9}  FG   {'Estado':<7}{'Carga':<7}Janela")
    for a in apps:
        win = ("minimizada" if a["windows_visible"] and a["windows_minimized"] == a["windows_visible"]
               else "visivel" if a["windows_visible"] else "sem janela")
        o.p(f"  {a['friendly'][:27]:<28}{a['pids'][0]:>7} {num(a['cpu_pct'], 0):>5}% {num(a['gpu_pct'], 0):>5}% "
            f"{num(a['ws_mb'], 0):>6} MB  {'YES' if a['foreground'] else 'NO ':<4} {a['state']:<7}{a['load']:<7}{win}")
    o.p(f"  ({len(f.apps)} grupos de executaveis; {pr.data.get('total_processes')} processos; "
        f"{pr.data.get('denied')} inacessiveis)   [fonte: psutil + PDH GPU Engine + user32]")
    o.p("  RAM = working set (inclui memoria compartilhada); CPU = % do total de CPU.")


def sec_eventlog(o, f, pipe):
    o.p("\nEvent Log (ultimos 7 dias, log System)")
    col = pipe.collectors["Event Log"]
    col.override_window_ms = 7 * 86400 * 1000
    col.seen.clear()
    res = col.run()
    col.override_window_ms = None
    f.results["Event Log"] = res
    if res.status != "OK":
        o.p(f"  {res.status}: {res.reason}")
        return res
    ev = res.data["events"]
    counts: dict[str, int] = {}
    for e in ev:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    o.p(f"  Eventos relevantes: {len(ev)} (de {res.data['scanned']} lidos)" + (f" -> {counts}" if counts else ""))
    for e in ev[:5]:
        o.p(f"    {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))}  {e['type']} ({e['severity']})  "
            f"{e['provider']} #{e['id']}")
    o.p("  [fonte: wevtutil / Windows Event Log]")
    return res


# ----------------------------------------------------------------------
# --diagnostic
# ----------------------------------------------------------------------
def run_diagnostic(args) -> int:
    cfg = make_cfg(args)
    root = Path(args.root)
    o = Out()
    pipe = Pipeline(cfg)
    o.p(f"NotebookAI Diagnostic — v{VERSION}")
    o.p(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')} | Python {platform.python_version()} | {platform.platform()}")
    o.p(f"Processo elevado (administrador): {'SIM' if pipe.elevated else 'NAO'}")
    if not winapi.IS_WIN:
        o.p("AVISO: este nao e Windows. Os coletores Windows ficam UNAVAILABLE; isso e esperado fora do Windows.")
    o.p("Coletando (3 amostras, ~6 s; contadores de taxa precisam de duas leituras)...")
    pipe.collectors["LibreHardwareMonitor"].dedupe = False    # diagnostico pontual: sempre emite o snapshot valido
    frames = []
    try:
        pipe.start(wait_first_s=cfg.lhm_prime_secs)           # espera LIMITADA pela 1a leitura do LHM
        for i in range(3):
            frames.append(pipe.tick(force_all=True))
            if i < 2:
                time.sleep(2.2)
    finally:
        pipe.close()                                          # encerra a thread do poller (idempotente)
    f = frames[-1]
    f.results = dict(pipe.last)

    sec_cpu(o, f); sec_gpu(o, f); sec_ram(o, f); sec_ssd(o, f); sec_battery(o, f, cfg)
    sec_foreground(o, f)
    o.p("\nProcessos (TOP 10 por CPU+GPU suavizados)")
    apps_table(o, f)
    evres = sec_eventlog(o, f, pipe)

    # --- autoteste SQLite e motor de intervalos (com dados reais coletados acima) ---
    sqlite_ok, sqlite_reason = True, ""
    try:
        from agent.store import Store
        st = Store(root / "Data" / "notebookai.db")
        st.meta_set("diagnostic_last_run", time.strftime("%Y-%m-%d %H:%M:%S"))
        st.commit()
        back = st.meta_get("diagnostic_last_run")
        sqlite_ok = back is not None
        st.checkpoint_and_close()
    except Exception as e:  # noqa: BLE001
        sqlite_ok, sqlite_reason = False, f"{type(e).__name__}: {e}"
    engine_ok, engine_reason = True, ""
    tmp = Path(tempfile.mkdtemp(prefix="nai_diag_"))
    try:
        rec = Recorder(tmp, cfg)
        n = 0
        for fr in frames:
            s = to_sample(fr)
            n += 1 if s.values else 0
            rec.feed(s)
        rec.shutdown(frames[-1].ts + 2, "diagnostico")
        db = sqlite3.connect(tmp / "Data" / "notebookai.db")
        cnt = db.execute("select count(*) from intervals").fetchone()[0]
        db.close()
        if n == 0:
            engine_ok = None          # sem dados reais para alimentar o motor: nao testado (nao e falha do motor)
            engine_reason = "nenhum coletor produziu valores nesta execucao"
        else:
            engine_ok = cnt >= 1
            if not engine_ok:
                engine_reason = f"{cnt} intervalos gravados com {n} amostras com valores"
    except Exception as e:  # noqa: BLE001
        engine_ok, engine_reason = False, f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- relatorio ---
    o.p("\n" + "=" * 60)
    o.p("NotebookAI Diagnostic Report")
    o.p("=" * 60)
    hw = f.results.get("LibreHardwareMonitor")

    def tag(res):
        return "[OK]" if res.status == "OK" else f"[{res.status}] {res.reason}"

    o.p(f"\nLibreHardwareMonitor:\n{tag(hw)}")
    if hw.status == "OK":
        cats: dict[str, int] = {}
        for r in hw.readings:
            cats[r.category] = cats.get(r.category, 0) + 1
        mapped = sum(1 for r in hw.readings if not r.key.startswith("lhm."))
        o.p(f"\nSensors detected:\n{hw.data['n_readings']}  (sensores/grupos: {hw.data['n_sensors']}; "
            f"mapeados p/ chave canonica: {mapped}; preservados como lhm.*: {len(hw.readings) - mapped})")
        for c, label in (("cpu", "CPU"), ("gpu", "GPU"), ("ram", "RAM"), ("battery", "Battery"), ("ssd", "SSD")):
            o.p(f"\n{label} sensors:\n{cats.get(c, 0)}")
        if hw.data.get("stale_note"):
            o.p(f"\nAVISO: {hw.data['stale_note']}")
    rows = [("Windows CPU collector", "Windows CPU"), ("Windows RAM collector", "Windows RAM"),
            ("Windows GPU collector", "Windows GPU"), ("Windows SSD collector", "Windows SSD"),
            ("Windows process collector", "Processes"), ("Foreground collector", "Foreground"),
            ("Battery API", "Battery API"), ("Event Log", "Event Log")]
    for label, name in rows:
        o.p(f"\n{label}:\n{tag(f.results[name])}")
    o.p(f"\nSQLite:\n{'[OK]' if sqlite_ok else '[FAILED] ' + sqlite_reason}")
    o.p("\nInterval engine:\n" + ("[OK]" if engine_ok else
        ("[NOT TESTED] " + engine_reason if engine_ok is None else "[FAILED] " + engine_reason)))
    o.p("\nPresentMon:\nNOT IMPLEMENTED IN V0.1")

    # --- administrador: o que ESTA execucao provou ---
    o.p("\nPrecisa de administrador? (resultado desta execucao: "
        f"{'ELEVADO' if pipe.elevated else 'SEM elevacao'})")
    for name, col in pipe.collectors.items():
        o.p(f"  {name}: {f.results[name].status}   (expectativa: {col.admin_expected})")
    o.p("  Rode o diagnostico uma vez normal e outra como administrador e compare os dois relatorios.")

    core = ["Windows RAM", "Windows CPU", "Processes", "Foreground"]
    core_ok = all(f.results[c].status == "OK" for c in core) and sqlite_ok and engine_ok is True
    missing = [n for n in f.results if f.results[n].status != "OK"]
    if core_ok and not missing:
        overall = "READY FOR REAL-WORLD COLLECTION"
    elif core_ok:
        overall = "PARTIALLY READY (coleta funciona, mas sem: " + ", ".join(missing) + ")"
    else:
        overall = "FAILED — " + ", ".join(
            [c for c in core if f.results[c].status != "OK"] + ([] if sqlite_ok else ["SQLite"])
            + ([] if engine_ok is True else ["Interval engine (" + ("nao testado" if engine_ok is None else "falhou") + ")"]))
    o.p(f"\nOverall:\n{overall}")

    out = root / "Exports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"diagnostic_{time.strftime('%Y%m%d_%H%M%S')}_{'admin' if pipe.elevated else 'user'}.txt"
    path.write_text("\n".join(o.lines), encoding="utf-8")
    print(f"\nRelatorio salvo em: {path}")
    return 0 if core_ok else 1


# ----------------------------------------------------------------------
# --list-sensors
# ----------------------------------------------------------------------
def run_list_sensors(args) -> int:
    cfg = make_cfg(args)
    pipe = Pipeline(cfg)
    try:
        pipe.start(wait_first_s=cfg.lhm_prime_secs)           # espera LIMITADA pela 1a leitura do LHM
        res = pipe.collectors["LibreHardwareMonitor"].run()
    finally:
        pipe.close()
    if res.status != "OK":
        print(f"LibreHardwareMonitor {res.status}: {res.reason}")
        print("Nenhum sensor listado (nada e inventado). Veja README: como configurar o LibreHardwareMonitor.")
        return 2
    groups: dict[str, dict[str, list]] = {}
    for r in res.readings:
        groups.setdefault(r.category, {}).setdefault(r.group or "(derivado)", []).append(r)
    print(f"LibreHardwareMonitor sensors detected: {res.data['n_readings']}  (grupos: {res.data['n_sensors']}; "
          f"{res.data['version']})")
    flt = (args.filter or "").lower()
    for cat in sorted(groups):
        print(f"\n[{cat.upper()}]")
        for grp, rs in groups[cat].items():
            shown = [r for r in rs if not flt or flt in r.label.lower() or flt in grp.lower() or flt in r.key]
            if not shown:
                continue
            print(f"  {grp}")
            for r in shown:
                mapped = "" if r.key.startswith("lhm.") else "  *"
                val = fmt_reading(r) if r.value is not None else "N/D"
                print(f"    {r.label:<42}{val:>16}   -> {r.key}{mapped}   (id {r.sensor_id})")
    print("\n* = mapeado para chave canonica; sem * = preservado como lhm.<categoria>.<hardware>.<sensor>.<tipo>")
    return 0


# ----------------------------------------------------------------------
# --monitor
# ----------------------------------------------------------------------
def summary_line(f: Frame) -> str:
    def g(k, d=0, suf=""):
        r = f.readings.get(k)
        return "N/D" if r is None or r.value is None else f"{r.value:.{d}f}{suf}"

    ac = f.readings.get("battery.ac")
    acs = "AC N/D" if ac is None or ac.value is None else ("AC ON" if ac.value else "AC OFF")
    app = f.foreground["friendly"] if f.foreground else "N/D"
    return (f"{time.strftime('%H:%M:%S', time.localtime(f.ts))} | CPU {g('cpu.util_pct', 0, '%')} | "
            f"GPU {g('gpu.util_pct', 0, '%')} | RAM {g('ram.used_gb', 1, 'GB')} | "
            f"CPU {g('cpu.package_temp_c', 0, '°C')} | GPU {g('gpu.temp_c', 0, '°C')} | {acs} | {app}")


def run_monitor(args) -> int:
    cfg = make_cfg(args)
    root = Path(args.root)

    def on_interval(c):
        iv = c["interval"]
        print(f"   >> intervalo fechado {time.strftime('%H:%M:%S', time.localtime(iv['start']))}–"
              f"{time.strftime('%H:%M:%S', time.localtime(iv['end']))} | {iv['state']} | "
              f"{iv.get('fg_app')} | corte: {iv['reason']}")

    def on_event(ts, e):
        print(f"   !! evento {e['type']} ({e['severity']}) {e['detail']}")

    rec = Recorder(root, cfg, on_interval, on_event)
    pipe = Pipeline(cfg)
    end = time.time() + args.minutes * 60 if args.minutes else None
    print(f"NotebookAI monitor — gravando em {root.resolve()} | elevado: {'SIM' if pipe.elevated else 'NAO'} | "
          f"{'ate Ctrl+C' if end is None else f'{args.minutes} min'}")
    last_print = 0.0
    snap = {"full": None, "status": None, "ts": 0.0}
    discrepancy_checked = False
    try:
        pipe.start(wait_first_s=cfg.lhm_prime_secs)           # espera LIMITADA pela 1a leitura do LHM
        if end is not None:
            end = time.time() + args.minutes * 60             # a espera acima nao consome a duracao pedida
        while end is None or time.time() < end:
            t0 = time.time()
            f = pipe.tick(t0)
            for name, old, new, reason in f.status_changes:
                print(f"   ## {name}: {old or '-'} -> {new}" + (f" ({reason})" if reason else ""))
            if f.foreground:
                rec.register_app(f.foreground["friendly"], f.foreground["path"])
            extra = []
            for e in f.events:
                if e["severity"]:
                    extra.append({"ts": e["ts"], "type": e["type"], "severity": e["severity"],
                                  "detail": {"provider": e["provider"], "event_id": e["id"], **e["data"]}})
            rec.feed(to_sample(f), extra)
            # --- bateria: snapshots e validacao da design capacity ---
            b = f.results.get("Battery API")
            if b and b.status == "OK" and "Battery API" in f.ran:
                v = {k: f.readings[k].value for k in ("battery.design_mwh", "battery.full_mwh",
                                                       "battery.remaining_mwh", "battery.voltage_mv",
                                                       "battery.rate_mw", "battery.cycles", "battery.health_pct")
                     if k in f.readings}
                if (v.get("battery.full_mwh") != snap["full"] or b.data["status"] != snap["status"]
                        or f.ts - snap["ts"] >= 3600):
                    ac = f.readings.get("battery.ac")
                    rec.store.save_battery_snapshot(f.ts, {
                        "design_mwh": v.get("battery.design_mwh"), "full_mwh": v.get("battery.full_mwh"),
                        "remaining_mwh": v.get("battery.remaining_mwh"), "voltage_mv": v.get("battery.voltage_mv"),
                        "rate_mw": v.get("battery.rate_mw"), "status": b.data["status"],
                        "ac": None if ac is None else int(ac.value), "cycles": v.get("battery.cycles"),
                        "health_pct": v.get("battery.health_pct")})
                    snap.update(full=v.get("battery.full_mwh"), status=b.data["status"], ts=f.ts)
                rc = b.data.get("reference_check")
                if rc and not discrepancy_checked:
                    discrepancy_checked = True
                    if abs(rc["diff_pct"]) >= 1 and rec.store.meta_get("design_discrepancy_logged") != str(rc["detected_mwh"]):
                        rec._emit(f.ts, {"type": "battery_design_discrepancy", "severity": "info",
                                         "detail": {k: rc[k] for k in rc}})
                        rec.store.meta_set("design_discrepancy_logged", rc["detected_mwh"])
            if t0 - last_print >= args.print_every:
                print(summary_line(f))
                last_print = t0
            time.sleep(max(0.0, cfg.sample_secs - (time.time() - t0)))
        reason = "monitor_finalizado"
    except KeyboardInterrupt:
        reason = "monitor_interrompido_ctrl_c"
    finally:
        pipe.close()                                          # encerra a thread do poller (idempotente)
    rec.shutdown(time.time(), reason)
    print(f"Encerrado ({reason}). Banco: {root / 'Data' / 'notebookai.db'} | TXT: {root / 'Logs'}")
    return 0


# ----------------------------------------------------------------------
# --apps
# ----------------------------------------------------------------------
def run_apps(args) -> int:
    pipe = Pipeline(make_cfg(args))
    end = time.time() + args.seconds
    print(f"Observando aplicativos por {args.seconds}s (alterne janelas; minimize o jogo; etc.). Ctrl+C encerra.")
    last = 0.0
    try:
        pipe.start()
        while time.time() < end:
            t0 = time.time()
            f = pipe.tick(t0)
            if args.watch and f.apps:
                for a in f.apps:
                    if args.watch.lower() in a["friendly"].lower() or args.watch.lower() in a["exe"].lower():
                        win = ("minimizada" if a["windows_visible"] and a["windows_minimized"] == a["windows_visible"]
                               else "visivel" if a["windows_visible"] else "sem janela")
                        print(f"{time.strftime('%H:%M:%S')} | {a['friendly']} | {a['state']} | LOAD {a['load']} | "
                              f"CPU {a['cpu_pct']:.0f}% GPU {a['gpu_pct']:.0f}% RAM {a['ws_mb']:.0f} MB | "
                              f"FG {'yes' if a['foreground'] else 'no'} | {win}")
            elif not args.watch and t0 - last >= 3:
                o = Out()
                o.p(f"\n{time.strftime('%H:%M:%S')}  Foreground: "
                    + (f"{f.foreground['friendly']} ({f.foreground['exe']}, PID {f.foreground['pid']})"
                       if f.foreground else "N/D"))
                apps_table(o, f)
                last = t0
            time.sleep(max(0.0, 2.0 - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        pipe.close()                                          # encerra a thread do poller (idempotente)
    return 0


# ----------------------------------------------------------------------
def make_cfg(args) -> Config:
    cfg = Config()
    if getattr(args, "nominal_voltage_mv", None):
        cfg.battery_nominal_voltage_mv = args.nominal_voltage_mv
    if getattr(args, "reference_design_mwh", None):
        cfg.battery_reference_design_mwh = args.reference_design_mwh
    return cfg


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="NotebookAIAgent", description="NotebookAI Agent (desenvolvimento)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--diagnostic", action="store_true", help="relatorio de diagnostico com valores reais")
    g.add_argument("--list-sensors", action="store_true", help="lista todos os sensores do LibreHardwareMonitor")
    g.add_argument("--monitor", action="store_true", help="coleta continua gravando em SQLite/TXT")
    g.add_argument("--apps", action="store_true", help="tabela de aplicativos (aberto x causando carga)")
    ap.add_argument("--root", default="NotebookAI_runtime", help="pasta de Data/Logs/Exports")
    ap.add_argument("--minutes", type=float, default=3, help="--monitor: duracao (0 = ate Ctrl+C)")
    ap.add_argument("--print-every", type=float, default=5, help="--monitor: segundos entre linhas")
    ap.add_argument("--seconds", type=float, default=60, help="--apps: duracao")
    ap.add_argument("--watch", help="--apps: acompanhar um app (parte do nome), ex.: minecraft")
    ap.add_argument("--filter", help="--list-sensors: filtra por texto")
    ap.add_argument("--nominal-voltage-mv", type=int, help="tensao nominal do pacote para 'Estimated mAh'")
    ap.add_argument("--reference-design-mwh", type=int, help="valor de referencia da Design Capacity (validacao)")
    args = ap.parse_args(argv)
    if args.diagnostic:
        return run_diagnostic(args)
    if args.list_sensors:
        return run_list_sensors(args)
    if args.monitor:
        return run_monitor(args)
    return run_apps(args)


if __name__ == "__main__":
    sys.exit(main())
