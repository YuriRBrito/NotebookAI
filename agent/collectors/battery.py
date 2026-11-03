"""Bateria real via Windows Battery API (IOCTL) + GetSystemPowerStatus.

Nada aqui usa 58.003 mWh como valor: a capacidade de projeto e LIDA do firmware.
O valor de referencia do usuario so aparece em `data['reference_check']`.
"""
from agent import winapi
from agent.readings import Collector, CollectorResult, Reading, Unavailable, nd

SRC_IOCTL = "Windows Battery IOCTL"
SRC_SPS = "Windows GetSystemPowerStatus"


def compute_battery(info: dict, st: dict, sps: dict, est_ioctl_s, nominal_mv=None, reference_mwh=None):
    """Funcao pura (testavel). Retorna (readings, data)."""
    R: list[Reading] = []
    data: dict = {}
    design, full = info["design_mwh"], info["full_mwh"]
    rem, volt, rate = st["remaining_mwh"], st["voltage_mv"], st["rate_mw"]

    if info["relative_capacity"]:
        data["warning"] = ("o firmware reporta capacidade RELATIVA (nao em mWh); "
                           "valores de capacidade nao sao energia absoluta")

    R.append(Reading("battery.design_mwh", float(design), "mWh", SRC_IOCTL, "Design Capacity"))
    R.append(Reading("battery.full_mwh", float(full), "mWh", SRC_IOCTL, "Full Charge Capacity"))
    if rem is not None:
        R.append(Reading("battery.remaining_mwh", float(rem), "mWh", SRC_IOCTL, "Remaining / Current Capacity"))
    if design:
        health = full / design * 100
        R.append(Reading("battery.health_pct", health, "%", "Windows IOCTL (calculado)", "Health",
                         kind="calculated", note="Full Charge / Design"
                         + ("; >100% indica firmware reportando acima do projeto" if health > 100 else "")))
        R.append(Reading("battery.wear_pct", 100 - health, "%", "Windows IOCTL (calculado)", "Wear",
                         kind="calculated", note="100 - Health"))
    if rem is not None and full:
        R.append(Reading("battery.charge_calc_pct", rem / full * 100, "%", "Windows IOCTL (calculado)",
                         "Charge (Remaining/Full)", kind="calculated"))
    if sps.get("battery_pct") is not None:
        R.append(Reading("battery.pct", float(sps["battery_pct"]), "%", SRC_SPS, "Charge level"))
    if volt is not None:
        R.append(Reading("battery.voltage_mv", float(volt), "mV", SRC_IOCTL, "Voltage"))
    if rate is not None:
        R.append(Reading("battery.rate_mw", float(rate), "mW", SRC_IOCTL, "Rate",
                         note="positivo = carregando, negativo = descarregando"))
        R.append(Reading("battery.power_w", abs(rate) / 1000.0, "W", SRC_IOCTL, "Power (|Rate|)",
                         kind="calculated"))
        if volt:
            R.append(Reading("battery.current_ma", rate / volt * 1000.0, "mA",
                             "Windows IOCTL (calculado)", "Current", kind="calculated",
                             note="Rate(mW) / Voltage(mV), ambos instantaneos; sem medicao direta de corrente"))
    R.append(Reading("battery.cycles", float(info["cycles"]), "", SRC_IOCTL, "Cycle count",
                     note="0 pode significar que o firmware nao informa ciclos" if info["cycles"] == 0 else ""))

    # AC / estado
    ac = sps.get("ac")
    if ac is not None:
        R.append(Reading("battery.ac", 1.0 if ac else 0.0, "", SRC_SPS, "AC connected"))
    if st["charging"]:
        status = "Charging"
    elif st["discharging"]:
        status = "Discharging"
    elif ac and rem is not None and full and rem >= 0.97 * full:
        status = "Full"
    elif ac:
        status = "AC connected, not charging"
    else:
        status = "Unknown"
    data["status"] = status
    data["ac_ioctl_online"] = st["online"]

    # tempo estimado
    if est_ioctl_s is not None:
        R.append(Reading("battery.est_remaining_s_ioctl", float(est_ioctl_s), "s", SRC_IOCTL, "Estimated remaining"))
    if sps.get("life_secs") is not None:
        R.append(Reading("battery.est_remaining_s_windows", float(sps["life_secs"]), "s", SRC_SPS,
                         "Estimated remaining (Windows)"))
    if st["discharging"] and rate and rem:
        R.append(Reading("battery.est_remaining_s_calc", rem / abs(rate) * 3600, "s",
                         "Windows IOCTL (calculado)", "Remaining (linear)", kind="estimated",
                         note="Remaining / |Rate| no ritmo atual; assume descarga constante"))
    if st["charging"] and rate and rate > 0 and rem is not None and full:
        R.append(Reading("battery.est_to_full_s_calc", max(0.0, (full - rem) / rate * 3600), "s",
                         "Windows IOCTL (calculado)", "Time to full (linear)", kind="estimated",
                         note="(Full - Remaining) / Rate no ritmo atual; a carga real desacelera perto de 100%"))

    # mAh: so com tensao NOMINAL informada pelo usuario; nunca com a tensao instantanea
    if nominal_mv:
        R.append(Reading("battery.estimated_full_mah", full / (nominal_mv / 1000.0), "mAh",
                         "calculado (mWh / tensao nominal informada pelo usuario)", "Estimated mAh",
                         kind="estimated", note=f"usa {nominal_mv} mV informados na configuracao"))
    else:
        R.append(nd("battery.estimated_full_mah", "mAh", "-",
                    "a Battery API entrega mWh, nao mAh. Converter exige a tensao NOMINAL do pacote, "
                    "que a API nao informa; a tensao instantanea nao serve. Informe "
                    "battery_nominal_voltage_mv na configuracao para obter 'Estimated mAh'.",
                    "Capacity (mAh)"))

    if reference_mwh:
        data["reference_check"] = {"detected_mwh": design, "reference_mwh": reference_mwh,
                                   "diff_mwh": design - reference_mwh,
                                   "diff_pct": (design - reference_mwh) / reference_mwh * 100}
    return R, data


class BatteryCollector(Collector):
    name = "Battery API"
    admin_expected = "nao (IOCTL em dispositivo de bateria costuma funcionar sem elevacao); a verificar"

    def __init__(self, nominal_mv=None, reference_mwh=None):
        self.dev = None
        self.nominal_mv = nominal_mv
        self.reference_mwh = reference_mwh

    def collect(self) -> CollectorResult:
        winapi.require_windows()
        sps = winapi.system_power_status()
        if self.dev is None:
            self.dev = winapi.BatteryDevice()
        try:
            info = self.dev.information()
            st = self.dev.status()
            try:
                est = self.dev.estimated_time_secs()
            except OSError:
                est = None
        except OSError as e:
            self.dev.close()
            self.dev = None            # reabre na proxima rodada
            raise Unavailable(f"consulta a bateria falhou: {e}")
        R, data = compute_battery(info, st, sps, est, self.nominal_mv, self.reference_mwh)
        data.update({"name": self.dev.text(4), "manufacturer": self.dev.text(6),
                     "chemistry": info["chemistry"], "batteries_found": self.dev.count})
        res = CollectorResult(self.name, readings=R, data=data)
        return res
