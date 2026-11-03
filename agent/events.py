"""Detector de eventos criticos com histerese (evento so na borda de subida)."""
from common.config import Config
from agent.intervals import Sample


class EventDetector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.active: set[str] = set()
        self.prev_discrete: dict[str, str] = {}

    def _edge(self, name: str, condition: bool) -> bool:
        if condition and name not in self.active:
            self.active.add(name)
            return True
        if not condition:
            self.active.discard(name)
        return False

    def check(self, s: Sample) -> list[dict]:
        """Retorna lista de {type, severity, detail}. Se nao vazia, o chamador
        deve fechar o intervalo atual imediatamente."""
        v, ev = s.values, []
        ctx = {"app": s.discrete.get("state.fg_app")}

        def emit(t, sev, **d):
            ev.append({"type": t, "severity": sev, "detail": {**d, **ctx}})

        for key, label in (("cpu.thermal_throttle", "thermal_throttling_cpu"),
                           ("gpu.thermal_throttle", "thermal_throttling_gpu"),
                           ("cpu.power_limit", "power_limit_cpu"),
                           ("cpu.edp_limit", "current_edp_limit_cpu")):
            if self._edge(label, bool(v.get(key))):
                emit(label, "critico", cpu_temp=v.get("cpu.package_temp_c"),
                     cpu_clock_mhz=v.get("cpu.clock_mhz"))

        for key, prof in self.cfg.profiles.items():
            if prof.critical is not None and key in v:
                if self._edge("alta_" + key, v[key] >= prof.critical):
                    emit("temperatura_muito_alta" if "temp" in key else "valor_critico",
                         "critico", sensor=key, value=v[key], limite=prof.critical)

        if "ram.used_pct" in v and self._edge("ram_alta", v["ram.used_pct"] > self.cfg.ram_critical_pct):
            emit("ram_acima_de_90", "critico", ram_pct=v["ram.used_pct"],
                 ram_gb=v.get("ram.used_gb"))
        if "ram.commit_pct" in v and self._edge("commit_alto", v["ram.commit_pct"] > 90):
            emit("memoria_comprometida_alta", "critico", commit_pct=v["ram.commit_pct"])

        bp = v.get("battery.pct")
        if bp is not None and self._edge(
                "bateria_baixa", bp <= self.cfg.battery_critical_pct and s.discrete.get("state.ac") != "1"):
            emit("bateria_5pct", "critico", pct=bp, flush=True)

        # mudancas de estado de energia: evento informativo + corte
        ac = s.discrete.get("state.ac")
        if "state.ac" in self.prev_discrete and ac != self.prev_discrete["state.ac"]:
            emit("carregador_conectado" if ac == "1" else "carregador_desconectado",
                 "critico", battery_pct=bp)
        self.prev_discrete["state.ac"] = ac
        return ev
