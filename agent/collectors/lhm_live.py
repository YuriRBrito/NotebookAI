"""Coletor LibreHardwareMonitor do Pipeline (Stage 3): consome o snapshot do LhmPoller.

Substitui o LhmCollector (WMI via PowerShell + HTTP) no fluxo principal; lhm.py continua existindo.
  * collect() NUNCA faz rede, PowerShell nem espera: so le poller.get() (a thread propria do poller e quem
    consulta http://localhost:8085/data.json a cada ~2 s).
  * Leituras: lhm_tree.to_rows(snapshot) -> lhm.build_readings (as MESMAS chaves/unidades/categorias de antes).

Politica por estado do poller (nada antigo sai como atual; sem leitura inventada):
  NO_DATA                      -> UNAVAILABLE "aguardando primeira leitura"
  FAILED                       -> UNAVAILABLE com tipo/texto do erro
  STALE (idade > stale_after)  -> UNAVAILABLE com idade e limite (o dado antigo NAO e emitido)
  FRESH, sequence nova         -> OK com as leituras  (data["snapshot"] == "new")
  FRESH, sequence ja emitida   -> OK SEM leituras     (data["snapshot"] == "repeat")
      O snapshot ainda e valido, mas ja virou amostra num tick anterior: emiti-lo de novo contaria a mesma
      medicao duas vezes no IntervalEngine (n, medias). Ficar OK (e nao UNAVAILABLE) evita falso "defeito"
      e ruido de status_changes; so nao entram chaves lhm.* neste tick, caso ja suportado pelo motor.
"""
import threading
import time
from typing import Callable

from agent.collectors import lhm_poller as P
from agent.collectors import lhm_tree
from agent.collectors.lhm import build_readings, lhm_running
from agent.readings import Collector, CollectorResult, Unavailable

SOURCE = "LibreHardwareMonitor (HTTP)"      # igual ao que o coletor antigo gravava via HTTP
PRIME_MAX_S = 30.0                          # teto absoluto da espera de prime(), seja qual for o pedido


class LhmPollerCollector(Collector):
    name = "LibreHardwareMonitor"
    admin_expected = ("o LHM costuma precisar de administrador para ler CPU/SSD; o Agent so le o HTTP local "
                      "(thread propria) e pode ficar sem elevacao (a verificar)")

    def __init__(self, url: str = P.DEFAULT_URL, *, interval_s: float = 2.0, timeout_s: float = 3.0,
                 stale_after_s: float = 6.0, poller: P.LhmPoller | None = None, dedupe: bool = True,
                 autostart: bool = True):
        self.url = url
        self.poller = poller or P.LhmPoller(url, interval_s=interval_s, timeout_s=timeout_s,
                                            stale_after_s=stale_after_s)
        self.dedupe = dedupe              # False: sempre emite o snapshot valido (diagnostico pontual)
        self.autostart = autostart        # False: so testes (poller dirigido manualmente)
        self._lock = threading.Lock()     # protege start/close (a leitura do tick nao usa lock)
        self._closed = False
        self._last_seq = 0
        self._last_counts = (0, 0)        # (n_sensors, n_readings) da ultima emissao

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        """Inicia a thread UMA vez (idempotente). Reabre apos close()."""
        with self._lock:
            self._closed = False
            if not self.poller.running:
                self.poller.start()

    def close(self) -> bool:
        """Encerra a thread (idempotente). True = encerrou. Depois disso collect() nao reinicia sozinho."""
        with self._lock:
            self._closed = True
            return self.poller.stop()

    def prime(self, timeout_s: float, *, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> P.PollState:
        """Inicia o poller e espera, no MAXIMO timeout_s (limitado a PRIME_MAX_S), pela primeira leitura.

        Retorna cedo se chegar um snapshot (FRESH) ou se a primeira tentativa falhar (LHM fechado/sem
        resposta: esperar mais nao ajuda; o poller continua tentando em segundo plano com backoff).
        Nunca levanta e nunca excede o limite: serve ao CLI, jamais ao tick.
        """
        self.start()
        limit = max(0.0, min(float(timeout_s), PRIME_MAX_S))
        deadline = clock() + limit
        while True:
            state = self.poller.get().state
            if state in (P.PollState.FRESH, P.PollState.FAILED, P.PollState.STALE):
                return state
            remaining = deadline - clock()
            if remaining <= 0:
                return state
            sleep(min(0.02, remaining))

    # ------------------------------------------------------------------ coleta (chamada pelo tick)
    def _data(self, r: P.PollResult, snapshot: str, counts: tuple[int, int]) -> dict:
        return {"n_sensors": counts[0], "n_readings": counts[1], "version": SOURCE, "stale_note": "",
                "process_running": True,            # respondeu por HTTP local => o LHM esta no ar
                "snapshot": snapshot, "sequence": r.sequence, "age_s": r.age_s, "fetched_at": r.fetched_at,
                "poller_state": r.state.value}

    def collect(self) -> CollectorResult:
        if self._closed:
            raise Unavailable("coletor do LibreHardwareMonitor encerrado (Pipeline.close())")
        if self.autostart and not self.poller.running:
            self.start()
        r = self.poller.get()
        last = f"[{r.error_kind}] {r.last_error}" if r.last_error else ""
        if r.state is P.PollState.NO_DATA:
            raise Unavailable(f"aguardando a primeira leitura do LibreHardwareMonitor ({self.url})")
        if r.state is P.PollState.FAILED:
            why = "LibreHardwareMonitor.exe NAO esta em execucao; " if not lhm_running() else ""
            raise Unavailable(f"{why}HTTP {self.url}: {last}")
        if r.state is P.PollState.STALE:
            raise Unavailable(f"dados do LibreHardwareMonitor com {r.age_s:.1f} s (limite "
                              f"{self.poller.stale_after_s:g} s); nao emitidos como atuais"
                              + (f"; ultimo erro: {last}" if last else ""))
        # FRESH
        if self.dedupe and r.sequence == self._last_seq:
            return CollectorResult(self.name, readings=[], data=self._data(r, "repeat", self._last_counts))
        readings = build_readings(lhm_tree.to_rows(r.tree.sensors), SOURCE)
        counts = (len({x.group for x in readings}), len(readings))
        self._last_seq, self._last_counts = r.sequence, counts
        return CollectorResult(self.name, readings=readings, data=self._data(r, "new", counts))
# FIM lhm_live.py