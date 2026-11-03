"""Motor de intervalos inteligentes.

Entrada: amostras internas (a cada ~2 s). Saida: intervalos fechados com min/max/avg
por sensor. Nada e persistido por amostra.

Regras:
  - Mudanca pequena: continua o intervalo.
  - Mudanca significativa: faixa (max-min) de um sensor guia passa de `significant`
    e permanece assim por `confirm_secs` -> fecha no instante em que o desvio comecou.
    Picos mais curtos que `confirm_secs` ficam dentro do intervalo (aparecem no max).
  - Estado discreto (app em primeiro plano, AC, perfil): mudanca estavel por
    `discrete_debounce_secs` fecha o intervalo.
  - Critico: o chamador usa force_close() e o intervalo fecha imediatamente.
  - Teto de duracao e virada do dia fecham o intervalo.
"""
import time
from dataclasses import dataclass, field

from common.config import Config, DISCRETE_KEYS


@dataclass
class Sample:
    ts: float
    values: dict[str, float]
    discrete: dict[str, str]
    meta: dict[str, dict] = field(default_factory=dict)  # label/unit/source por chave


EMA_ALPHA = 0.2   # ~10 s de memoria com amostras a 2 s


def _day(ts: float) -> int:
    t = time.localtime(ts)
    return t.tm_year * 1000 + t.tm_yday


class IntervalEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cur: dict | None = None
        self.stats: dict[str, dict] = {}
        self.meta: dict[str, dict] = {}
        self.last_ts: float | None = None
        self.pending: list[Sample] = []
        self.pending_since: float | None = None
        self.pending_need: float = 0.0
        self.ema: dict[str, float] = {}

    # ---- estatisticas -------------------------------------------------
    @staticmethod
    def _acc(stats: dict, s: Sample):
        for k, v in s.values.items():
            if v is None:
                continue
            st = stats.get(k)
            if st is None:
                stats[k] = {"min": v, "max": v, "sum": v, "n": 1}
            else:
                st["min"] = min(st["min"], v)
                st["max"] = max(st["max"], v)
                st["sum"] += v
                st["n"] += 1

    def _open(self, s: Sample, reason_prev: str = ""):
        self.cur = {"start": s.ts, "discrete": dict(s.discrete),
                    "fg_app": s.discrete.get("state.fg_app"),
                    "ac": s.discrete.get("state.ac")}
        self.stats = {}
        self._acc(self.stats, s)
        self.meta.update(s.meta)
        self.pending, self.pending_since = [], None
        self.ema = {k: v for k, v in s.values.items() if v is not None}

    def _close(self, end_ts: float, reason: str) -> dict:
        iv = dict(self.cur)
        iv["end"] = end_ts
        iv["reason"] = reason
        s = self.stats
        cpu = s.get("cpu.util_pct", {}).get("max", 0)
        gpu = s.get("gpu.util_pct", {}).get("max", 0)
        iv["state"] = "estavel" if cpu < 40 and gpu < 30 else "carga"
        out = {"interval": iv, "stats": s, "meta": self.meta}
        self.cur, self.stats = None, {}
        return out

    # ---- desvio -------------------------------------------------------
    def _deviation(self, s: Sample) -> float:
        """Retorna tempo de confirmacao exigido (0 = sem desvio)."""
        need = 0.0
        for k in DISCRETE_KEYS:
            if k in s.discrete and s.discrete[k] != self.cur["discrete"].get(k):
                need = max(need, self.cfg.discrete_debounce_secs) or 1e-9
        for k, prof in self.cfg.profiles.items():
            if not prof.guide:
                continue
            v = s.values.get(k)
            st = self.stats.get(k)
            if v is None or st is None:
                continue
            e = self.ema.get(k, v)
            e = e + EMA_ALPHA * (v - e)
            self.ema[k] = e
            ref = st["sum"] / st["n"]
            if abs(e - ref) > max(prof.small, prof.significant / 2):
                need = max(need, self.cfg.confirm_secs) or 1e-9
        return need

    # ---- API ----------------------------------------------------------
    def force_close(self, s: Sample, reason: str) -> list[dict]:
        """Fecha agora (evento critico/estado) e abre novo intervalo com `s`."""
        out = []
        if self.cur is not None:
            for p in self.pending:
                self._acc(self.stats, p)
            end = self.pending[-1].ts if self.pending else (
                self.last_ts if self.last_ts is not None else s.ts)
            out.append(self._close(end, reason))
        self._open(s)
        self.last_ts = s.ts
        return out

    def feed(self, s: Sample) -> list[dict]:
        out: list[dict] = []
        if self.cur is None:
            self._open(s)
            self.last_ts = s.ts
            return out

        # virada do dia e teto de duracao
        if _day(s.ts) != _day(self.cur["start"]):
            out += self.force_close(s, "virada_do_dia")
            return out
        if s.ts - self.cur["start"] > self.cfg.max_interval_secs:
            out += self.force_close(s, "teto_de_duracao")
            return out

        need = self._deviation(s)
        if need == 0.0:
            # voltou ao normal: amostras pendentes (pico curto) entram no intervalo
            for p in self.pending:
                self._acc(self.stats, p)
            self.pending, self.pending_since = [], None
            self._acc(self.stats, s)
            self.meta.update(s.meta)
            self.last_ts = s.ts
        else:
            if self.pending_since is None:
                self.pending_since = s.ts
            self.pending.append(s)
            self.pending_need = max(self.pending_need, need)
            if s.ts - self.pending_since >= need:
                buf = self.pending
                end = self.last_ts
                reason = "mudanca_de_estado" if any(
                    buf[-1].discrete.get(k) != self.cur["discrete"].get(k)
                    for k in DISCRETE_KEYS if k in buf[-1].discrete
                ) else "mudanca_significativa"
                out.append(self._close(end, reason))
                self._open(buf[0])
                for p in buf[1:]:
                    self._acc(self.stats, p)
                self.pending_need = 0.0
                self.last_ts = buf[-1].ts
        return out

    def close_current(self, ts: float, reason: str) -> list[dict]:
        """Desligamento/sleep/encerramento: fecha o que estiver aberto."""
        if self.cur is None:
            return []
        for p in self.pending:
            self._acc(self.stats, p)
        self.pending, self.pending_since = [], None
        return [self._close(ts, reason)]
