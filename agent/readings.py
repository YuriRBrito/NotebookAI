"""Tipos comuns. Toda leitura carrega a FONTE de onde veio."""
import time
from dataclasses import dataclass, field
from typing import Any


class Unavailable(Exception):
    """A fonte nao existe/esta fechada (nao e um erro do codigo)."""


@dataclass
class Reading:
    key: str                      # chave canonica ou hwinfo.<cat>.<grupo>.<sensor>
    value: float | None           # None = N/D
    unit: str = ""
    source: str = ""              # ex.: "HWiNFO", "Windows PDH", "Windows Battery IOCTL"
    label: str = ""
    kind: str = "measured"        # measured | calculated | estimated
    note: str = ""                # explica N/D ou calculo
    vmin: float | None = None
    vmax: float | None = None
    vavg: float | None = None
    sensor_id: str | None = None  # HWiNFO: "<dwSensorID>:<inst>:<readingID>"
    category: str = ""
    group: str = ""               # HWiNFO: nome do grupo/sensor de origem


@dataclass
class CollectorResult:
    name: str
    status: str = "OK"            # OK | UNAVAILABLE | FAILED
    reason: str = ""
    readings: list[Reading] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)   # dados estruturados extras
    elapsed_ms: float = 0.0


class Collector:
    """Base: cada coletor roda isolado; excecao vira FAILED/UNAVAILABLE, nunca derruba o Agent."""
    name = "collector"
    admin_expected = "nao"        # expectativa documentada; o diagnostico mostra o resultado real

    def collect(self) -> CollectorResult:   # implementado pelas subclasses
        raise NotImplementedError

    def run(self) -> CollectorResult:
        t0 = time.perf_counter()
        try:
            res = self.collect()
        except Unavailable as e:
            res = CollectorResult(self.name, "UNAVAILABLE", str(e))
        except Exception as e:  # noqa: BLE001 - isolamento intencional
            res = CollectorResult(self.name, "FAILED", f"{type(e).__name__}: {e}")
        res.name = self.name
        res.elapsed_ms = (time.perf_counter() - t0) * 1000
        return res


def nd(key: str, unit: str, source: str, note: str, label: str = "") -> Reading:
    """Leitura N/D explicita: nunca substitui silenciosamente por outra metrica."""
    return Reading(key=key, value=None, unit=unit, source=source, label=label or key, note=note)
