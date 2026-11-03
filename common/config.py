"""Configuracao e limiares do NotebookAI (todos ajustaveis)."""
from dataclasses import dataclass, field


@dataclass
class SensorProfile:
    small: float          # variacao dentro da qual nada muda
    significant: float    # faixa (max-min) que forca novo intervalo
    critical: float | None = None   # valor que gera evento critico (>=)
    guide: bool = True    # sensor "guia": participa da decisao de corte


# Sensores guia: decidem o corte dos intervalos.
DEFAULT_PROFILES: dict[str, SensorProfile] = {
    "cpu.util_pct":        SensorProfile(small=15, significant=30),
    "gpu.util_pct":        SensorProfile(small=15, significant=30),
    "ram.used_gb":         SensorProfile(small=0.5, significant=1.5),
    "cpu.package_temp_c":  SensorProfile(small=5, significant=10, critical=95),
    "gpu.temp_c":          SensorProfile(small=5, significant=10, critical=90),
    "cpu.package_power_w": SensorProfile(small=4, significant=10),
    "battery.pct":         SensorProfile(small=3, significant=8),
    "ssd.activity_pct":    SensorProfile(small=20, significant=40, guide=False),
    "cpu.clock_mhz":       SensorProfile(small=300, significant=900, guide=False),
}

# Estados discretos: qualquer mudanca fecha o intervalo (apos debounce).
DISCRETE_KEYS = ("state.fg_app", "state.ac", "state.power_profile")


@dataclass
class Config:
    profiles: dict[str, SensorProfile] = field(default_factory=lambda: dict(DEFAULT_PROFILES))
    confirm_secs: float = 10.0       # valor fora da banda precisa persistir por este tempo
    discrete_debounce_secs: float = 3.0
    max_interval_secs: float = 6 * 3600
    ram_critical_pct: float = 90.0
    battery_critical_pct: float = 5.0
    flush_secs: float = 30.0
    sample_secs: float = 2.0
    # --- coleta real ---
    process_secs: float = 5.0
    eventlog_secs: float = 60.0
    # Valor INFORMADO PELO USUARIO, usado somente para validar o que o firmware reporta.
    battery_reference_design_mwh: int = 58003
    # Tensao nominal do pacote (mV), se o usuario quiser "Estimated mAh". None = N/D.
    battery_nominal_voltage_mv: int | None = None
    # --- LibreHardwareMonitor via HTTP (poller em thread propria; o Pipeline so le o snapshot) ---
    lhm_url: str = "http://localhost:8085/data.json"
    lhm_poll_secs: float = 2.0       # intervalo de consulta do poller (independente da cadencia do Pipeline)
    lhm_timeout_secs: float = 3.0    # timeout de cada consulta HTTP
    lhm_stale_secs: float = 6.0      # snapshot mais velho que isso NAO e emitido como atual
    lhm_prime_secs: float = 4.0      # CLI: espera maxima pela 1a leitura antes de comecar (limitada a 30 s)