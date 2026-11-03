"""Testes do poller HTTP do LHM (etapa 2). Nao precisam do LibreHardwareMonitor aberto nem de Windows.

Estrategia: a logica (estados, stale, backoff) e testada com fetch/relogio/espera INJETADOS (zero espera real);
HTTP real so contra um servidor local em 127.0.0.1 com porta efemera (timeout de 0.2 s, recusa de conexao,
resposta valida/invalida). Threads sao sincronizadas por Event, nunca por sleep adivinhado; os limites de
tempo (5 s) existem so como rede de seguranca e nao sao atingidos quando tudo funciona.
O arquivo termina com a linha '# FIM test_lhm_poller.py' (confere se a copia veio completa).
"""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.collectors import lhm_poller as P
from agent.collectors import lhm_tree as T

SAFETY = 5.0          # limite maximo de espera em qualquer sincronizacao (nao e esperado atingir)


# ----------------------------------------------------------------------
# apoio
# ----------------------------------------------------------------------
def payload(n: int = 1) -> bytes:
    """data.json minimo e valido; o valor do unico sensor identifica a leitura (n)."""
    sensor = {"id": 3, "Text": "CPU Package", "Min": "", "Value": f"{n},0 \u00b0C", "Max": "",
              "SensorId": "/intelcpu/0/temperature/12", "Type": "Temperature", "ImageURL": "",
              "RawValue": f"{n},0 \u00b0C", "Children": []}
    group = {"id": 2, "Text": "Temperatures", "Min": "", "Value": "", "Max": "", "ImageURL": "", "Children": [sensor]}
    hw = {"id": 1, "Text": "Intel Core i5", "Min": "", "Value": "", "Max": "", "HardwareId": "/intelcpu/0",
          "ImageURL": "", "Children": [group]}
    comp = {"id": 0, "Text": "PC", "Min": "", "Value": "", "Max": "", "ImageURL": "", "Children": [hw]}
    root = {"id": 0, "Text": "Sensor", "Min": "Min", "Value": "Value", "Max": "Max", "ImageURL": "",
            "Children": [comp]}
    return json.dumps(root).encode("utf-8")


def temp_of(tree) -> float:
    return T.select(tree.sensors, stype="Temperature")[0].value


class Clock:
    """Relogio falso: monotonico e de parede andam juntos."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


class Script:
    """fetch falso: devolve/levanta, em ordem, os itens da lista (o ultimo se repete)."""

    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    def __call__(self):
        i = min(self.calls, len(self.items) - 1)
        self.calls += 1
        it = self.items[i]
        if isinstance(it, BaseException):
            raise it
        return it


def make(fetch, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("interval_s", 2.0)
    kw.setdefault("timeout_s", 3.0)
    kw.setdefault("backoff_max_s", 30.0)
    return clock, P.LhmPoller(fetch=fetch, clock=clock, wall_clock=clock, **kw)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        srv = self.server
        srv.hits += 1
        if srv.mode == "slow":
            srv.release.wait(SAFETY)                    # segura a resposta ate o teardown
        if srv.mode == "http500":
            self.send_response(500)
            self.end_headers()
            return
        body = srv.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *a):                          # silencioso
        pass


class LocalServer:
    def __init__(self, body=b"", mode="ok"):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.daemon_threads = True
        self.srv.body, self.srv.mode, self.srv.hits = body, mode, 0
        self.srv.release = threading.Event()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/data.json"
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.release.set()
        self.srv.shutdown()
        self.srv.server_close()
        self.t.join(SAFETY)


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                           # nada escutando nessa porta
    return f"http://127.0.0.1:{port}/data.json"


# ----------------------------------------------------------------------
# estados e snapshot (sem thread, sem rede)
# ----------------------------------------------------------------------
def test_no_initial_snapshot():
    _, p = make(Script(payload(1)))
    r = p.get()
    assert r.state is P.PollState.NO_DATA and r.tree is None and r.fresh_tree is None
    assert r.age_s is None and r.fetched_at is None and r.sequence == 0 and not r.is_fresh
    assert r.last_error == "" and r.consecutive_failures == 0 and r.last_attempt_at is None
    assert not p.running
    print("OK test_no_initial_snapshot")


def test_valid_response_updates_snapshot():
    clk, p = make(Script(payload(41), payload(42)))
    assert p.poll_once() is True
    r = p.get()
    assert r.state is P.PollState.FRESH and r.is_fresh and r.sequence == 1
    assert temp_of(r.tree) == 41.0 and r.fresh_tree is r.tree
    assert r.fetched_at == 1000.0 and r.age_s == 0.0 and r.last_error == "" and r.error_kind == ""
    clk.advance(1.5)
    assert p.get().age_s == 1.5
    assert p.poll_once() is True                        # nova leitura substitui a anterior
    r2 = p.get()
    assert r2.sequence == 2 and temp_of(r2.tree) == 42.0 and r2.age_s == 0.0 and r2.fetched_at == 1001.5
    assert temp_of(r.tree) == 41.0                      # a foto antiga nao foi alterada (imutavel/atomica)
    print("OK test_valid_response_updates_snapshot")


def test_stale_detection():
    clk, p = make(Script(payload(1)), stale_after_s=10.0)
    p.poll_once()
    clk.advance(10.0)
    assert p.get().state is P.PollState.FRESH           # no limite ainda e fresco
    clk.advance(0.001)
    r = p.get()
    assert r.state is P.PollState.STALE and not r.is_fresh
    assert r.fresh_tree is None                         # dado antigo NUNCA sai como atual
    assert r.tree is not None and temp_of(r.tree) == 1.0    # mas continua disponivel p/ diagnostico
    assert r.age_s > 10.0
    p.poll_once()                                       # nova leitura volta a FRESH
    assert p.get().state is P.PollState.FRESH
    # padrao: 3 intervalos + timeout
    _, q = make(Script(payload(1)), interval_s=2.0, timeout_s=3.0)
    assert q.stale_after_s == 9.0
    print("OK test_stale_detection")


def test_invalid_json():
    for bad in (b"isto nao e json", b"[]", b"{}", b"\xff\xfe\x00", b""):
        _, p = make(Script(bad))
        assert p.poll_once() is False, bad
        r = p.get()
        assert r.state is P.PollState.FAILED and r.error_kind == P.ERR_FORMAT, bad
        assert r.tree is None and r.consecutive_failures == 1 and "payload invalido" in r.last_error
    # JSON invalido depois de uma leitura boa: o snapshot bom e MANTIDO (nao e sobrescrito por lixo)
    clk, p = make(Script(payload(7), b"lixo"), stale_after_s=10.0)
    p.poll_once()
    assert p.poll_once() is False
    r = p.get()
    assert temp_of(r.tree) == 7.0 and r.sequence == 1 and r.error_kind == P.ERR_FORMAT
    assert r.state is P.PollState.FRESH and r.consecutive_failures == 1     # ainda recente, mas a falha aparece
    clk.advance(11)
    r = p.get()
    assert r.state is P.PollState.STALE and r.fresh_tree is None and r.last_error
    print("OK test_invalid_json")


def test_failure_kinds_and_never_raises():
    cases = [(P.PollError(P.ERR_TIMEOUT, "t"), P.ERR_TIMEOUT), (TimeoutError("x"), P.ERR_TIMEOUT),
             (socket.timeout("x"), P.ERR_TIMEOUT), (ConnectionRefusedError("recusada"), P.ERR_CONNECTION),
             (OSError("boom"), P.ERR_CONNECTION), (P.PollError(P.ERR_HTTP, "h"), P.ERR_HTTP),
             (RuntimeError("bug no fetch"), P.ERR_OTHER), (KeyError("k"), P.ERR_OTHER)]
    for exc, kind in cases:
        _, p = make(Script(exc))
        assert p.poll_once() is False                   # nao levanta
        r = p.get()
        assert r.state is P.PollState.FAILED and r.error_kind == kind, (exc, r.error_kind)
        assert r.last_error and r.tree is None
    # excecao dentro do parser injetado tambem e isolada
    _, p = make(Script(payload(1)), parse=lambda raw: 1 / 0)
    assert p.poll_once() is False and p.get().error_kind == P.ERR_OTHER
    print("OK test_failure_kinds_and_never_raises")


def test_recovery_after_failure():
    clk, p = make(Script(ConnectionRefusedError("x"), ConnectionRefusedError("x"), payload(5)))
    assert not p.poll_once() and not p.poll_once()
    r = p.get()
    assert r.state is P.PollState.FAILED and r.consecutive_failures == 2 and r.sequence == 0
    assert p.next_delay() == 4.0
    assert p.poll_once()
    r = p.get()
    assert r.state is P.PollState.FRESH and temp_of(r.tree) == 5.0 and r.sequence == 1
    assert r.consecutive_failures == 0 and r.last_error == "" and r.error_kind == ""
    assert p.next_delay() == 2.0                        # backoff zerado
    print("OK test_recovery_after_failure")


def test_exponential_backoff():
    _, p = make(Script(OSError("x"), OSError("x"), OSError("x"), OSError("x"), OSError("x"), OSError("x"),
                       payload(1)), interval_s=2.0, backoff_max_s=30.0, backoff_factor=2.0)
    assert p.next_delay() == 2.0                        # sem falhas: intervalo normal
    seen = []
    for _ in range(6):
        p.poll_once()
        seen.append(p.next_delay())
    assert seen == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0], seen     # dobra e para no teto
    p.poll_once()                                       # sucesso
    assert p.next_delay() == 2.0
    # fator customizado
    _, q = make(Script(OSError("x")), interval_s=1.0, backoff_max_s=100.0, backoff_factor=3.0)
    out = []
    for _ in range(4):
        q.poll_once()
        out.append(q.next_delay())
    assert out == [1.0, 3.0, 9.0, 27.0], out
    # parametros invalidos
    for kw in ({"interval_s": 0}, {"timeout_s": -1}, {"backoff_factor": 0.5}, {"backoff_max_s": 1.0},
               {"stale_after_s": 0}):
        try:
            make(Script(payload()), **kw)
        except ValueError:
            pass
        else:
            raise AssertionError(kw)
    print("OK test_exponential_backoff")


# ----------------------------------------------------------------------
# thread
# ----------------------------------------------------------------------
def test_thread_loop_uses_backoff_without_real_waiting():
    """A thread real aplica next_delay() entre consultas. wait() falso: registra o atraso e nao dorme."""
    delays, done = [], threading.Event()

    def fake_wait(d):
        delays.append(d)
        if len(delays) >= 7:
            done.set()
            return True                                 # pede para parar
        return False

    fetch = Script(OSError("x"), OSError("x"), OSError("x"), payload(9), payload(10), OSError("x"), payload(11))
    _, p = make(fetch, wait=fake_wait)
    p.start()
    assert done.wait(SAFETY)
    assert p.stop(SAFETY) is True and not p.running
    # 3 falhas (2,4,8) -> sucesso (2) -> sucesso (2) -> falha (2) -> sucesso (2)
    assert delays == [2.0, 4.0, 8.0, 2.0, 2.0, 2.0, 2.0], delays
    assert fetch.calls == 7 and p.get().sequence == 3
    print("OK test_thread_loop_uses_backoff_without_real_waiting")


def test_thread_start_stop_and_restart():
    reached = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        reached.set()
        return payload(len(calls))

    _, p = make(fetch, interval_s=0.01, backoff_max_s=0.05, timeout_s=1.0)
    assert not p.running
    p.start()
    assert p.running and reached.wait(SAFETY)
    try:
        p.start()
    except RuntimeError:
        pass
    else:
        raise AssertionError("start duplo deveria falhar")
    t = p._thread
    assert t.daemon and t.name == "lhm-poller"
    assert p.stop(SAFETY) is True
    assert not p.running and not t.is_alive()
    n = len(calls)
    time.sleep(0.05)                                    # curto: so confere que nada novo ocorre apos o stop
    assert len(calls) == n, "a thread continuou consultando apos stop()"
    assert p.stop() is True                             # stop repetido e inofensivo
    reached.clear()
    p.start()                                           # reinicio permitido
    assert reached.wait(SAFETY) and p.stop(SAFETY)
    with P.LhmPoller(fetch=lambda: payload(3), interval_s=0.01, backoff_max_s=0.05) as q:   # context manager
        assert q.running
    assert not q.running
    names = [t.name for t in threading.enumerate()]
    assert "lhm-poller" not in names, names             # nenhuma thread orfa
    print("OK test_thread_start_stop_and_restart")


def test_stop_is_prompt_while_waiting_between_polls():
    """Com intervalo enorme, stop() acorda a espera (Event.wait) em vez de aguardar o intervalo."""
    first = threading.Event()

    def fetch():
        first.set()
        return payload(1)

    p = P.LhmPoller(fetch=fetch, interval_s=3600.0, backoff_max_s=3600.0)
    p.start()
    assert first.wait(SAFETY)
    t0 = time.monotonic()
    assert p.stop(SAFETY) is True
    assert time.monotonic() - t0 < 2.0 and not p.running
    print("OK test_stop_is_prompt_while_waiting_between_polls")


def test_consumer_not_blocked_by_slow_fetch():
    """Enquanto o fetch esta preso (servidor lento), get() responde na hora."""
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(SAFETY)
        return payload(1)

    p = P.LhmPoller(fetch=slow, interval_s=0.01, backoff_max_s=0.05, timeout_s=SAFETY)
    p.start()
    try:
        assert entered.wait(SAFETY)                     # a thread esta DENTRO do fetch
        t0 = time.monotonic()
        for _ in range(1000):
            r = p.get()
        assert time.monotonic() - t0 < 1.0              # 1000 consultas sem esperar o fetch
        assert r.state is P.PollState.NO_DATA
    finally:
        release.set()
        assert p.stop(SAFETY)
    assert p.get().state is P.PollState.FRESH           # e depois da liberacao o dado chega
    print("OK test_consumer_not_blocked_by_slow_fetch")


def test_snapshot_race_consistency():
    """Leitores martelando get() enquanto a thread publica: cada foto deve ser coerente (arvore == sequence)."""
    counter = {"n": 0}

    def fetch():
        counter["n"] += 1                               # so a thread do poller chama fetch
        return payload(counter["n"])

    p = P.LhmPoller(fetch=fetch, interval_s=0.0005, backoff_max_s=0.001, stale_after_s=60.0)
    stop = threading.Event()
    errors, reads = [], [0]
    lock = threading.Lock()

    def reader():
        local = 0
        last_seq = 0
        while not stop.is_set():
            r = p.get()
            local += 1
            if r.tree is None:
                if r.sequence != 0:
                    errors.append(("arvore ausente com sequence", r.sequence))
                continue
            if temp_of(r.tree) != float(r.sequence):    # arvore e contador publicados juntos
                errors.append(("incoerente", temp_of(r.tree), r.sequence))
            if r.sequence < last_seq:
                errors.append(("sequence recuou", last_seq, r.sequence))
            last_seq = r.sequence
            time.sleep(0)                                # cede a vez (GIL) para a thread do poller
        with lock:
            reads[0] += local

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    p.start()
    for t in readers:
        t.start()
    deadline = time.monotonic() + SAFETY
    while p.get().sequence < 100 and time.monotonic() < deadline:
        time.sleep(0.001)
    stop.set()
    for t in readers:
        t.join(SAFETY)
    assert p.stop(SAFETY)
    assert not errors, errors[:3]
    assert p.get().sequence >= 100 and reads[0] > 0
    print(f"OK test_snapshot_race_consistency (sequence={p.get().sequence}, leituras={reads[0]})")


# ----------------------------------------------------------------------
# HTTP real, somente contra servidor local efemero
# ----------------------------------------------------------------------
def test_http_valid_response():
    with LocalServer(payload(33)) as s:
        p = P.LhmPoller(s.url, timeout_s=2.0)
        assert p.poll_once() is True
        r = p.get()
        assert r.state is P.PollState.FRESH and temp_of(r.tree) == 33.0 and s.srv.hits == 1
        # BOM (o LHM/PowerShell as vezes o enviam) tambem passa pelo parser existente
        s.srv.body = b"\xef\xbb\xbf" + payload(34)
        assert p.poll_once() and temp_of(p.get().tree) == 34.0
    print("OK test_http_valid_response")


def test_http_invalid_json_and_http_error():
    with LocalServer(b"<html>nao e json</html>") as s:
        p = P.LhmPoller(s.url, timeout_s=2.0)
        assert p.poll_once() is False
        assert p.get().error_kind == P.ERR_FORMAT and p.get().state is P.PollState.FAILED
    with LocalServer(payload(1), mode="http500") as s:
        p = P.LhmPoller(s.url, timeout_s=2.0)
        assert p.poll_once() is False
        r = p.get()
        assert r.error_kind == P.ERR_HTTP and "500" in r.last_error
    print("OK test_http_invalid_json_and_http_error")


def test_http_timeout():
    with LocalServer(payload(1), mode="slow") as s:
        p = P.LhmPoller(s.url, timeout_s=0.2)
        t0 = time.monotonic()
        assert p.poll_once() is False
        dt = time.monotonic() - t0
        r = p.get()
        assert r.error_kind == P.ERR_TIMEOUT and "timeout" in r.last_error, r
        assert 0.15 <= dt < 3.0, dt                     # respeitou o timeout configurado (0,2 s)
    print(f"OK test_http_timeout ({dt:.2f} s)")


def test_http_connection_failure_and_recovery():
    # Falha de conexão determinística, independente do comportamento
    # do Windows ao acessar uma porta local sem servidor.
    def fail_connection():
        raise OSError("conexao recusada simulada")

    p = P.LhmPoller(
        url="http://127.0.0.1:1/data.json",
        timeout_s=1.0,
        fetch=fail_connection,
    )

    assert p.poll_once() is False
    r = p.get()
    assert r.state is P.PollState.FAILED
    assert r.error_kind == P.ERR_CONNECTION
    assert r.last_error
    assert r.consecutive_failures == 1

    # Recuperação: o mesmo poller passa a ler um servidor HTTP real.
    with LocalServer(payload(77)) as s:
        p.url = s.url
        p._fetch = lambda: P.http_fetch(p.url, p.timeout_s)

        assert p.poll_once() is True
        r = p.get()
        assert r.state is P.PollState.FRESH
        assert temp_of(r.tree) == 77.0
        assert r.consecutive_failures == 0

    print("OK test_http_connection_failure_and_recovery")


def test_http_ignores_environment_proxy():
    import os
    old = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy")}
    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://127.0.0.1:9"      # proxy inexistente
    try:
        with LocalServer(payload(2)) as s:
            assert P.LhmPoller(s.url, timeout_s=2.0).poll_once() is True
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("OK test_http_ignores_environment_proxy")


def test_http_end_to_end_with_thread():
    """Thread + HTTP real local: a primeira leitura chega, o servidor muda, a leitura acompanha."""
    with LocalServer(payload(1)) as s:
        with P.LhmPoller(s.url, interval_s=0.01, backoff_max_s=0.05, timeout_s=1.0) as p:
            deadline = time.monotonic() + SAFETY
            while p.get().sequence < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert temp_of(p.get().tree) == 1.0
            s.srv.body = payload(2)
            while temp_of(p.get().tree) != 2.0 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert temp_of(p.get().tree) == 2.0 and p.get().state is P.PollState.FRESH
        assert not p.running
    print("OK test_http_end_to_end_with_thread")


def test_real_sample_through_poller():
    """O data.json REAL do notebook (se existir) passa pelo poller + parser existente."""
    f = Path(__file__).resolve().parent / "data" / "lhm_data_sample.json"
    if not f.exists():
        print("SKIP test_real_sample_through_poller (tests/data/lhm_data_sample.json ausente)")
        return
    _, p = make(Script(f.read_bytes()))
    assert p.poll_once() is True
    tr = p.get().tree
    assert len(tr.sensors) == 160 and len(tr.hardware) == 13
    print("OK test_real_sample_through_poller (160 sensores, 13 hardwares)")


if __name__ == "__main__":
    test_no_initial_snapshot()
    test_valid_response_updates_snapshot()
    test_stale_detection()
    test_invalid_json()
    test_failure_kinds_and_never_raises()
    test_recovery_after_failure()
    test_exponential_backoff()
    test_thread_loop_uses_backoff_without_real_waiting()
    test_thread_start_stop_and_restart()
    test_stop_is_prompt_while_waiting_between_polls()
    test_consumer_not_blocked_by_slow_fetch()
    test_snapshot_race_consistency()
    test_http_valid_response()
    test_http_invalid_json_and_http_error()
    test_http_timeout()
    test_http_connection_failure_and_recovery()
    test_http_ignores_environment_proxy()
    test_http_end_to_end_with_thread()
    test_real_sample_through_poller()
    print("OK test_lhm_poller")
# FIM test_lhm_poller.py