"""Poller HTTP do LibreHardwareMonitor (etapa 2).

Consulta periodicamente http://localhost:8085/data.json em UMA thread propria, interpreta a resposta com o
parser puro (lhm_tree.parse_tree) e guarda a ultima arvore valida em memoria. Quem consome (futuramente o
Pipeline.tick()) so le o snapshot: get() nunca faz rede e nunca espera por I/O.

Responsabilidades:
  * este modulo: HTTP, agendamento, backoff, snapshot, estado (fresco/antigo/sem dado/falha);
  * lhm_tree.py: interpretar o JSON (injetado em `parse`, padrao lhm_tree.parse_tree).

Estados devolvidos por get() (PollState):
  FRESH   - ha leitura valida E ela e recente (idade <= stale_after_s).
  STALE   - ha leitura valida, mas antiga. Os dados vem junto (para diagnostico), porem o estado avisa.
            Se a ultima tentativa falhou, last_error explica o motivo.
  NO_DATA - nunca houve leitura valida e nenhuma tentativa falhou ainda (nao iniciado / primeira consulta).
  FAILED  - nunca houve leitura valida e a(s) tentativa(s) falharam (last_error / error_kind explicam).
Uma falha isolada logo apos uma leitura recente continua FRESH (o dado ainda e recente);
consecutive_failures/last_error mostram que a ultima consulta falhou.
Dados antigos nunca sao apresentados como atuais: PollResult.fresh_tree e None fora de FRESH.

Sem subprocessos, sem WMI, sem dependencias externas (so biblioteca padrao). Nao integrado ao Pipeline.
"""
import enum
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from agent.collectors import lhm_tree

DEFAULT_URL = "http://localhost:8085/data.json"
MAX_BODY_BYTES = 32 * 1024 * 1024      # protecao: resposta absurdamente grande nao entra na memoria

# categorias de erro (PollResult.error_kind)
ERR_TIMEOUT = "timeout"
ERR_CONNECTION = "connection"
ERR_HTTP = "http"
ERR_FORMAT = "format"
ERR_OTHER = "other"


class PollState(enum.Enum):
    FRESH = "fresh"
    STALE = "stale"
    NO_DATA = "no_data"
    FAILED = "failed"


class PollError(Exception):
    """Falha de uma consulta. `kind` e uma das constantes ERR_*."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class PollResult:
    """Foto imutavel do estado do poller no instante de get(). `tree` NAO deve ser alterada pelo consumidor."""
    state: PollState
    tree: "lhm_tree.LhmTree | None"      # ultima arvore valida (pode ser antiga: ver state)
    fetched_at: float | None             # time.time() da ultima leitura valida
    age_s: float | None                  # idade da leitura valida, medida no instante de get()
    sequence: int                        # quantas leituras validas ate agora (0 = nenhuma)
    consecutive_failures: int
    last_error: str                      # texto da ultima falha ('' se a ultima tentativa deu certo)
    error_kind: str                      # ERR_* da ultima falha ('' se nenhuma)
    last_attempt_at: float | None        # time.time() da ultima tentativa (sucesso ou falha)

    @property
    def is_fresh(self) -> bool:
        return self.state is PollState.FRESH

    @property
    def fresh_tree(self) -> "lhm_tree.LhmTree | None":
        """A arvore SOMENTE se for recente; senao None (nunca devolve dado antigo como atual)."""
        return self.tree if self.state is PollState.FRESH else None


def http_fetch(url: str, timeout: float, max_bytes: int = MAX_BODY_BYTES,
               clock: Callable[[], float] = time.monotonic) -> bytes:
    """GET simples com timeout TOTAL (nao so por operacao de socket). Levanta PollError.

    Ignora proxies do ambiente (HTTP_PROXY etc.): o alvo e o localhost e nao deve passar por proxy.
    """
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise PollError(ERR_OTHER, f"URL nao suportada: {url!r}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = clock() + timeout
    try:
        with opener.open(url, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise PollError(ERR_HTTP, f"HTTP {status} em {url}")
            chunks, size = [], 0
            while True:
                if clock() > deadline:
                    raise PollError(ERR_TIMEOUT, f"timeout de {timeout:g} s lendo {url}")
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise PollError(ERR_HTTP, f"resposta maior que {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
    except PollError:
        raise
    except urllib.error.HTTPError as e:
        raise PollError(ERR_HTTP, f"HTTP {e.code} {e.reason} em {url}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise PollError(ERR_TIMEOUT, f"timeout de {timeout:g} s em {url}") from e
        raise PollError(ERR_CONNECTION, f"sem conexao com {url}: {e.reason}") from e
    except (socket.timeout, TimeoutError) as e:
        raise PollError(ERR_TIMEOUT, f"timeout de {timeout:g} s em {url}") from e
    except OSError as e:                      # reset, aborto, etc.
        raise PollError(ERR_CONNECTION, f"erro de conexao com {url}: {e}") from e
    except ValueError as e:                   # URL malformada
        raise PollError(ERR_OTHER, f"URL invalida {url!r}: {e}") from e


class LhmPoller:
    """Thread que atualiza um snapshot com a ultima arvore valida do LHM.

    Todos os colaboradores sao injetaveis (testes sem rede e sem esperas reais):
      fetch()      -> bytes   (padrao: http_fetch(url, timeout)); pode levantar qualquer excecao
      parse(raw)   -> LhmTree (padrao: lhm_tree.parse_tree); LhmFormatError/ValueError = payload invalido
      clock()      -> float   relogio monotonico (idade/stale)
      wall_clock() -> float   relogio de parede (timestamps informativos)
      wait(delay)  -> bool    espera `delay` s; True = pediram para parar (padrao: Event.wait)
    """

    def __init__(self, url: str = DEFAULT_URL, *, interval_s: float = 2.0, timeout_s: float = 3.0,
                 backoff_max_s: float = 30.0, backoff_factor: float = 2.0,
                 stale_after_s: float | None = None,
                 fetch: Callable[[], bytes] | None = None,
                 parse: Callable[[bytes], "lhm_tree.LhmTree"] = lhm_tree.parse_tree,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 wait: Callable[[float], bool] | None = None):
        if interval_s <= 0 or timeout_s <= 0:
            raise ValueError("interval_s e timeout_s devem ser > 0")
        if backoff_factor < 1:
            raise ValueError("backoff_factor deve ser >= 1")
        if backoff_max_s < interval_s:
            raise ValueError("backoff_max_s deve ser >= interval_s")
        self.url = url
        self.interval_s = float(interval_s)
        self.timeout_s = float(timeout_s)
        self.backoff_max_s = float(backoff_max_s)
        self.backoff_factor = float(backoff_factor)
        # sem valor explicito: 3 intervalos + o tempo de uma consulta lenta
        self.stale_after_s = float(stale_after_s) if stale_after_s is not None \
            else 3 * self.interval_s + self.timeout_s
        if self.stale_after_s <= 0:
            raise ValueError("stale_after_s deve ser > 0")
        self._fetch = fetch or (lambda: http_fetch(self.url, self.timeout_s))
        self._parse = parse
        self._clock = clock
        self._wall = wall_clock
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._lock = threading.Lock()                 # protege TODOS os campos _s_* abaixo
        self._thread: threading.Thread | None = None
        self._s_tree: lhm_tree.LhmTree | None = None
        self._s_mono: float | None = None
        self._s_wall: float | None = None
        self._s_seq = 0
        self._s_failures = 0
        self._s_error = ""
        self._s_kind = ""
        self._s_attempt: float | None = None

    # ------------------------------------------------------------------ consulta (consumidor)
    def get(self) -> PollResult:
        """Foto atual. Nunca faz rede nem espera I/O: so segura o lock para copiar alguns campos."""
        now = self._clock()
        with self._lock:
            tree, mono = self._s_tree, self._s_mono
            age = None if mono is None else max(0.0, now - mono)
            if tree is None:
                state = PollState.FAILED if self._s_failures else PollState.NO_DATA
            elif age is not None and age <= self.stale_after_s:
                state = PollState.FRESH
            else:
                state = PollState.STALE
            return PollResult(state=state, tree=tree, fetched_at=self._s_wall, age_s=age, sequence=self._s_seq,
                              consecutive_failures=self._s_failures, last_error=self._s_error,
                              error_kind=self._s_kind, last_attempt_at=self._s_attempt)

    # ------------------------------------------------------------------ uma consulta
    def poll_once(self) -> bool:
        """Faz UMA consulta (chamada pela thread; publica em teste). True = leitura valida publicada.

        Nunca levanta: qualquer falha vira estado (last_error/error_kind) e conta para o backoff.
        """
        attempt_wall = self._wall()
        try:
            raw = self._fetch()
            try:
                tree = self._parse(raw)
            except ValueError as e:               # LhmFormatError herda de ValueError
                raise PollError(ERR_FORMAT, f"payload invalido: {e}") from e
        except PollError as e:
            self._record_failure(e.kind, str(e), attempt_wall)
            return False
        except (socket.timeout, TimeoutError) as e:
            self._record_failure(ERR_TIMEOUT, f"timeout: {e}", attempt_wall)
            return False
        except OSError as e:
            self._record_failure(ERR_CONNECTION, f"erro de conexao: {e}", attempt_wall)
            return False
        except Exception as e:                    # noqa: BLE001 - a thread nunca pode morrer
            self._record_failure(ERR_OTHER, f"{type(e).__name__}: {e}", attempt_wall)
            return False
        mono = self._clock()
        with self._lock:                          # publicacao atomica: arvore + tempos + contador juntos
            self._s_tree, self._s_mono, self._s_wall = tree, mono, self._wall()
            self._s_seq += 1
            self._s_failures = 0
            self._s_error = self._s_kind = ""
            self._s_attempt = attempt_wall
        return True

    def _record_failure(self, kind: str, message: str, attempt_wall: float) -> None:
        with self._lock:
            self._s_failures += 1
            self._s_error, self._s_kind, self._s_attempt = message, kind, attempt_wall

    def next_delay(self) -> float:
        """Espera ate a proxima consulta: intervalo normal; apos falhas, backoff exponencial limitado."""
        with self._lock:
            n = self._s_failures
        if n <= 0:
            return self.interval_s
        return min(self.backoff_max_s, self.interval_s * (self.backoff_factor ** (n - 1)))

    # ------------------------------------------------------------------ ciclo de vida
    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> None:
        """Inicia a thread (daemon). Levanta RuntimeError se ja estiver rodando. Pode reiniciar apos stop()."""
        if self.running:
            raise RuntimeError("LhmPoller ja esta em execucao")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lhm-poller", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float | None = None) -> bool:
        """Pede o encerramento e espera a thread terminar. True = encerrou.

        A thread so pode demorar se estiver no meio de uma consulta HTTP; isso e limitado por timeout_s.
        Por isso o padrao de espera e timeout_s + 1.
        """
        self._stop.set()
        t = self._thread
        if t is None:
            return True
        if t is not threading.current_thread():
            t.join(self.timeout_s + 1.0 if join_timeout is None else join_timeout)
        if t.is_alive():
            return False
        self._thread = None
        return True

    def __enter__(self) -> "LhmPoller":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        # primeira consulta imediata; depois espera (intervalo ou backoff) entre consultas
        while not self._stop.is_set():
            self.poll_once()
            if self._wait(self.next_delay()) or self._stop.is_set():
                break
# FIM lhm_poller.py