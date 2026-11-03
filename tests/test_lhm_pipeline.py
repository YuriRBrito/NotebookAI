"""Testes de integracao do Stage 3: LhmPoller dentro do Pipeline. Sem LHM aberto, sem Windows.

Os coletores Windows sao substituidos por falsos (chaves reais do projeto); o LHM usa um LhmPoller REAL com
fetch/relogio injetados (deterministico) ou um servidor HTTP local em 127.0.0.1 (porta efemera).
O arquivo termina com a linha '# FIM test_lhm_pipeline.py' (confere se a copia veio completa).
"""
import argparse
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent.cli as cli
from agent.collectors import lhm_live as LL
from agent.collectors import lhm_poller as P
from agent.collectors import lhm_tree as T
from agent.collectors.lhm import build_readings
from agent.pipeline import Pipeline, to_sample
from agent.readings import Collector, CollectorResult, Reading
from agent.recorder import Recorder
from common.config import Config

SAFETY = 5.0          # limite maximo de espera em sincronizacoes (rede de seguranca; nao e esperado atingir)
NAME = "LibreHardwareMonitor"
SAMPLE = Path(__file__).resolve().parent / "data" / "lhm_data_sample.json"
T0 = 1_800_000_000.0


# ----------------------------------------------------------------------
# apoio proprio (este arquivo nao depende de test_lhm_poller.py)
# ----------------------------------------------------------------------
def payload(n: int = 1) -> bytes:
    """data.json minimo e valido; o valor do unico sensor (CPU Package) identifica a leitura (n)."""
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


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        srv = self.server
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
    """Servidor HTTP em 127.0.0.1, porta efemera. mode: ok | slow (nunca responde ate o fim) | http500.
    Falha 'deterministica': http500 responde na hora em qualquer SO (nao depende de recusa de conexao)."""

    def __init__(self, body=b"", mode="ok"):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.daemon_threads = True
        self.srv.body, self.srv.mode = body, mode
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


# ----------------------------------------------------------------------
# apoio
# ----------------------------------------------------------------------
class FakeWin(Collector):
    """Coletor 'Windows' falso, com chaves reais do projeto."""
    name = "Windows CPU"

    def __init__(self):
        self.n = 0

    def collect(self):
        self.n += 1
        rs = [Reading("cpu.util_pct", 10.0 + self.n, "%", "Windows PDH", "CPU", category="cpu"),
              Reading("ram.used_gb", 8.5, "GB", "Windows", "RAM usada", category="ram")]
        return CollectorResult(self.name, readings=rs)


def lhm_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "lhm-poller" and t.is_alive()]


def manual_pipeline(fetch, clock=None, stale=6.0, **ckw):
    """Pipeline com cadencias originais; LHM com poller dirigido por poll_once() (sem thread)."""
    clock = clock or Clock()
    poller = P.LhmPoller(fetch=fetch, clock=clock, wall_clock=clock, interval_s=2.0, timeout_s=3.0,
                         stale_after_s=stale)
    col = LL.LhmPollerCollector(poller=poller, autostart=False, **ckw)
    pipe = Pipeline(Config())
    pipe.collectors = {NAME: col, "Windows CPU": FakeWin()}
    pipe.every = {NAME: 3.0}                           # cadencia historica do LHM
    return pipe, col, poller, clock


def sample_bytes() -> bytes:
    return SAMPLE.read_bytes() if SAMPLE.exists() else payload(55)


@contextlib.contextmanager
def real_pipeline(url, **cfg_kw):
    cfg = Config()
    cfg.lhm_url = url
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    pipe = Pipeline(cfg)
    try:
        yield pipe
    finally:
        pipe.close()


# ----------------------------------------------------------------------
# ciclo de vida
# ----------------------------------------------------------------------
def test_pipeline_wiring_and_defaults():
    pipe = Pipeline(Config())
    col = pipe.collectors[NAME]
    assert isinstance(col, LL.LhmPollerCollector) and col.name == NAME
    assert list(pipe.collectors)[0] == NAME                       # mesma ordem/precedencia de antes
    assert pipe.every[NAME] == 3.0                                # cadencia historica preservada
    p = col.poller
    assert (p.interval_s, p.timeout_s, p.stale_after_s) == (2.0, 3.0, 6.0)
    assert not lhm_threads()                                      # construir NAO inicia thread
    print("OK test_pipeline_wiring_and_defaults")


def test_start_once_no_duplicate_threads_and_close():
    base = len(lhm_threads())
    with LocalServer(mode="http500") as srv, real_pipeline(srv.url, lhm_timeout_secs=0.5) as pipe:
        pipe.start()
        pipe.start()                                              # idempotente
        pipe.collectors[NAME].start()
        pipe.tick(T0)                                             # tick tambem nao cria outra
        pipe.tick(T0 + 4)
        assert len(lhm_threads()) == base + 1, lhm_threads()
        first = pipe.collectors[NAME].poller._thread
        pipe.close()
        assert len(lhm_threads()) == base and not first.is_alive()
        pipe.close()                                              # close repetido e inofensivo
        f = pipe.tick(T0 + 8)                                     # fechado: nao reinicia sozinho
        assert len(lhm_threads()) == base
        assert f.results[NAME].status == "UNAVAILABLE" and "encerrado" in f.results[NAME].reason
        pipe.start()                                              # reinicio explicito permitido
        assert len(lhm_threads()) == base + 1
        pipe.close()
        assert len(lhm_threads()) == base
    print("OK test_start_once_no_duplicate_threads_and_close")


def test_context_manager_and_exception_close():
    base = len(lhm_threads())
    try:
        with Pipeline(Config()) as pipe:
            assert len(lhm_threads()) == base + 1
            raise RuntimeError("falha no meio do uso")
    except RuntimeError:
        pass
    assert len(lhm_threads()) == base
    print("OK test_context_manager_and_exception_close")


def test_tick_never_blocks_on_http():
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(SAFETY)
        return payload(1)

    poller = P.LhmPoller(fetch=slow, interval_s=2.0, timeout_s=SAFETY, stale_after_s=6.0)
    col = LL.LhmPollerCollector(poller=poller)                    # autostart: o PROPRIO tick inicia a thread
    pipe = Pipeline(Config())
    pipe.collectors = {NAME: col, "Windows CPU": FakeWin()}
    try:
        t0 = time.monotonic()
        f = pipe.tick(T0, force_all=True)
        assert entered.wait(SAFETY)                               # a thread esta presa no fetch
        for i in range(1, 6):
            pipe.tick(T0 + i * 3, force_all=True)
        assert time.monotonic() - t0 < 1.0, "tick esperou pelo HTTP"
        assert f.results[NAME].status == "UNAVAILABLE" and "aguardando" in f.results[NAME].reason
        assert "cpu.util_pct" in f.readings                       # Windows segue normal
    finally:
        release.set()
        pipe.close()
    assert not lhm_threads()
    print("OK test_tick_never_blocks_on_http")


# ----------------------------------------------------------------------
# prime(): espera limitada
# ----------------------------------------------------------------------
def test_prime_bounded():
    # 1) poller preso no fetch: prime respeita o limite pedido
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(SAFETY)
        return payload(1)

    col = LL.LhmPollerCollector(poller=P.LhmPoller(fetch=slow, timeout_s=SAFETY))
    t0 = time.monotonic()
    st = col.prime(0.2)
    dt = time.monotonic() - t0
    assert st is P.PollState.NO_DATA and 0.15 <= dt < 1.5, (st, dt)
    release.set()
    col.close()
    # 2) retorna CEDO quando chega o snapshot
    col = LL.LhmPollerCollector(poller=P.LhmPoller(fetch=lambda: payload(1), interval_s=0.05, backoff_max_s=0.1))
    t0 = time.monotonic()
    assert col.prime(10.0) is P.PollState.FRESH and time.monotonic() - t0 < 2.0
    col.close()
    # 3) retorna CEDO quando o LHM falha na 1a tentativa (aqui: HTTP 500), sem consumir os 10 s
    with LocalServer(mode="http500") as srv:
        col = LL.LhmPollerCollector(srv.url, timeout_s=1.0)
        t0 = time.monotonic()
        assert col.prime(10.0) is P.PollState.FAILED and time.monotonic() - t0 < 3.0
        col.close()
    # 4) teto absoluto: pedir 1000 s vira PRIME_MAX_S (relogio/sleep falsos, zero espera real)
    fake = Clock()

    def fsleep(d):
        fake.advance(d)

    col = LL.LhmPollerCollector(poller=P.LhmPoller(fetch=lambda: (release_forever.wait(SAFETY), payload(1))[1]))
    release_forever = threading.Event()
    st = col.prime(1000.0, clock=fake, sleep=fsleep)
    assert st is P.PollState.NO_DATA and LL.PRIME_MAX_S <= fake.now - 1000.0 < LL.PRIME_MAX_S + 0.1
    assert col.prime(-5.0, clock=fake, sleep=fsleep) is P.PollState.NO_DATA      # negativo = nao espera
    release_forever.set()
    col.close()
    assert not lhm_threads()
    print("OK test_prime_bounded")


# ----------------------------------------------------------------------
# leituras: chaves, metadados, Windows, duplicacao, None
# ----------------------------------------------------------------------
def test_readings_keys_and_metadata_match_legacy_mapping():
    raw = sample_bytes()
    pipe, col, poller, clk = manual_pipeline(Script(raw))
    assert poller.poll_once()
    f = pipe.tick(T0)
    res = f.results[NAME]
    assert res.status == "OK" and res.name == NAME
    expected = build_readings(T.to_rows(T.parse_tree(raw).sensors), "LibreHardwareMonitor (HTTP)")
    got = {r.key: r for r in res.readings}
    assert len(got) == len(res.readings) == len(expected)         # sem duplicatas
    for e in expected:                                            # chaves, valores, unidades, categorias...
        g = got[e.key]
        assert (g.value, g.unit, g.source, g.label, g.kind, g.note, g.vmin, g.vmax, g.sensor_id, g.category,
                g.group) == (e.value, e.unit, e.source, e.label, e.kind, e.note, e.vmin, e.vmax, e.sensor_id,
                             e.category, e.group), e.key
    assert all(f.readings[k] is got[k] for k in got)              # tudo chegou ao Frame
    if SAMPLE.exists():
        assert len(res.readings) == 162
        assert got["cpu.package_temp_c"].unit == "°C" and got["cpu.package_temp_c"].vmax is not None
        assert any(k.startswith("lhm.") for k in got) and "cpu.lhm.util_pct" in got
    # formato de res.data usado pelo CLI
    for k in ("n_sensors", "n_readings", "version", "stale_note", "process_running"):
        assert k in res.data, k
    assert res.data["n_readings"] == len(res.readings) and res.data["version"] == "LibreHardwareMonitor (HTTP)"
    assert res.data["n_sensors"] == len({r.group for r in res.readings}) and res.data["snapshot"] == "new"
    assert res.data["stale_note"] == "" and res.data["process_running"] is True
    print(f"OK test_readings_keys_and_metadata_match_legacy_mapping ({len(res.readings)} leituras)")


def test_windows_readings_untouched_and_separate():
    pipe, col, poller, clk = manual_pipeline(Script(sample_bytes()))
    poller.poll_once()
    f = pipe.tick(T0)
    assert f.readings["cpu.util_pct"].value == 11.0 and f.readings["cpu.util_pct"].source == "Windows PDH"
    assert f.readings["ram.used_gb"].value == 8.5
    win_keys = {"cpu.util_pct", "ram.used_gb"}
    lhm_keys = {r.key for r in f.results[NAME].readings}
    assert not (win_keys & lhm_keys)                              # nenhuma chave compartilhada
    assert all(f.readings[k].source.startswith("LibreHardwareMonitor") for k in lhm_keys)
    # LHM indisponivel nao derruba o Windows
    pipe2, _, p2, _ = manual_pipeline(Script(OSError("x")))
    p2.poll_once()
    f2 = pipe2.tick(T0)
    assert f2.results[NAME].status == "UNAVAILABLE" and f2.readings["cpu.util_pct"].value == 11.0
    print("OK test_windows_readings_untouched_and_separate")


def test_snapshot_emitted_once_not_duplicated():
    n = {"i": 0}

    def fetch():
        n["i"] += 1
        return payload(n["i"])

    pipe, col, poller, clk = manual_pipeline(fetch)
    poller.poll_once()                                            # snapshot 1
    f = pipe.tick(T0)
    r1 = f.results[NAME]
    assert r1.status == "OK" and r1.data["snapshot"] == "new" and r1.data["sequence"] == 1 and r1.readings
    clk.advance(4)                                                # poller parado: snapshot de 4 s ainda e VALIDO
    f = pipe.tick(T0 + 4)
    r2 = f.results[NAME]
    assert NAME in f.ran and r2.status == "OK" and r2.data["snapshot"] == "repeat" and r2.data["sequence"] == 1
    assert r2.readings == [] and not [k for k in f.readings if k.startswith(("lhm.", "cpu.package"))]
    assert r2.data["n_readings"] == r1.data["n_readings"]         # contagem da ultima emissao (para o CLI)
    assert f.status_changes == [] or all(c[0] != NAME for c in f.status_changes)    # sem ruido de status
    poller.poll_once()                                            # snapshot 2 -> volta a emitir
    f = pipe.tick(T0 + 8)
    r3 = f.results[NAME]
    assert r3.data["snapshot"] == "new" and r3.data["sequence"] == 2 and r3.readings
    assert [x.value for x in r3.readings if x.key == "cpu.package_temp_c"] == [2.0]
    # dedupe=False (diagnostico pontual): reemite o snapshot valido
    pipe2, col2, p2, c2 = manual_pipeline(Script(payload(3)), dedupe=False)
    p2.poll_once()
    assert pipe2.tick(T0, force_all=True).results[NAME].readings
    assert pipe2.tick(T0 + 1, force_all=True).results[NAME].data["snapshot"] == "new"
    print("OK test_snapshot_emitted_once_not_duplicated")


def test_none_is_not_zero_and_dropped_from_sample():
    raw = payload(5).replace(b"5,0 \\u00b0C", b"-")               # sensor sem valor ("-")
    assert b'"-"' in raw
    pipe, col, poller, clk = manual_pipeline(Script(raw))
    poller.poll_once()
    f = pipe.tick(T0)
    r = next(x for x in f.results[NAME].readings if x.key == "cpu.package_temp_c")
    assert r.value is None and r.value != 0 and r.note               # None continua None, com motivo
    s = to_sample(f)
    assert r.key not in s.values and r.key not in s.meta             # to_sample ignora N/D
    assert s.values["cpu.util_pct"] == 11.0                          # e o resto passa
    print("OK test_none_is_not_zero_and_dropped_from_sample")


# ----------------------------------------------------------------------
# estados: STALE / FAILED / NO_DATA nunca emitem dado antigo
# ----------------------------------------------------------------------
def test_stale_failed_nodata_emit_nothing(monkeypatch_running=None):
    LL.lhm_running = lambda: False                                # nao consulta processos reais
    pipe, col, poller, clk = manual_pipeline(Script(payload(9), OSError("recusada")), stale=6.0)
    # NO_DATA
    f = pipe.tick(T0)
    r = f.results[NAME]
    assert r.status == "UNAVAILABLE" and "aguardando" in r.reason and r.readings == []
    assert not any(k.startswith("lhm.") or k == "cpu.package_temp_c" for k in f.readings)
    # FRESH
    pipe.last_run.clear()
    poller.poll_once()
    f = pipe.tick(T0 + 4)
    assert f.results[NAME].status == "OK" and f.results[NAME].readings and "cpu.package_temp_c" in f.readings
    # STALE: sem novas leituras e idade > 6 s -> nada emitido, mesmo havendo dado em memoria
    clk.advance(6.5)
    assert poller.get().tree is not None                          # o poller AINDA guarda a arvore antiga
    f = pipe.tick(T0 + 8)
    r = f.results[NAME]
    assert r.status == "UNAVAILABLE" and "6.5" in r.reason and "nao emitidos como atuais" in r.reason
    assert r.readings == [] and not [k for k in f.readings if k.startswith("lhm.")]
    assert [c for c in f.status_changes if c[0] == NAME][0][1:3] == ("OK", "UNAVAILABLE")
    # FAILED depois de STALE (poller tem dado antigo E falhou): continua sem emitir, motivo traz o erro
    poller.poll_once()                                            # OSError
    f = pipe.tick(T0 + 12)
    r = f.results[NAME]
    assert r.status == "UNAVAILABLE" and "recusada" in r.reason and r.readings == []
    # FAILED puro (nunca houve dado)
    pipe2, _, p2, _ = manual_pipeline(Script(ConnectionRefusedError("recusada")))
    p2.poll_once()
    r = pipe2.tick(T0).results[NAME]
    assert r.status == "UNAVAILABLE" and "NAO esta em execucao" in r.reason and "[connection]" in r.reason
    print("OK test_stale_failed_nodata_emit_nothing")


def test_failure_and_recovery_through_pipeline():
    LL.lhm_running = lambda: False
    n = {"i": 0}
    script = [OSError("x"), OSError("x"), payload(1), payload(2)]

    def fetch():
        it = script[min(n["i"], len(script) - 1)]
        n["i"] += 1
        if isinstance(it, BaseException):
            raise it
        return it

    pipe, col, poller, clk = manual_pipeline(fetch)
    seen = []
    for i in range(4):
        poller.poll_once()
        clk.advance(2)
        f = pipe.tick(T0 + i * 4)
        seen.append(f.results[NAME].status)
        for c in f.status_changes:
            if c[0] == NAME:
                seen.append(c[1:3])
    assert seen == ["UNAVAILABLE", (None, "UNAVAILABLE"), "UNAVAILABLE", "OK", ("UNAVAILABLE", "OK"), "OK"], seen
    print("OK test_failure_and_recovery_through_pipeline")


# ----------------------------------------------------------------------
# persistencia: Sample -> IntervalEngine -> Recorder -> SQLite / TXT
# ----------------------------------------------------------------------
def test_persistence_sqlite_and_txt():
    LL.lhm_running = lambda: False
    n = {"i": 0}

    def fetch():
        n["i"] += 1
        if 12 <= n["i"] <= 15:                                    # queda do LHM no meio da gravacao
            raise ConnectionRefusedError("recusada")
        return sample_bytes() if SAMPLE.exists() else payload(40 + n["i"] % 5)

    pipe, col, poller, clk = manual_pipeline(fetch)
    tmp = Path(tempfile.mkdtemp(prefix="nai_t3_"))
    rec = Recorder(tmp)
    statuses = set()
    for i in range(60):                                           # 60 ticks de 2 s (2 min simulados)
        t = T0 + 2 * i
        clk.now = t                                               # relogio do poller acompanha o do pipeline
        poller.poll_once()
        f = pipe.tick(t)
        statuses.add(f.results[NAME].status if NAME in f.results else "-")
        rec.feed(to_sample(f))
    rec.shutdown(T0 + 125, "teste")
    db = sqlite3.connect(tmp / "Data" / "notebookai.db")
    srcs = dict(db.execute("select key, source from sensors").fetchall())
    assert "cpu.package_temp_c" in srcs and srcs["cpu.package_temp_c"] == "LibreHardwareMonitor (HTTP)"
    assert srcs["cpu.util_pct"] == "Windows PDH"                  # Windows separado
    assert db.execute("select count(*) from intervals").fetchone()[0] >= 1
    n_stats = db.execute("select max(n) from interval_stats s join sensors k on k.id=s.sensor_id "
                         "where k.key='cpu.package_temp_c'").fetchone()[0]
    assert n_stats is not None and n_stats <= 30                  # no maximo 1 amostra de LHM a cada ~4 s
    db.close()
    txt = "".join(p.read_text(encoding="utf-8") for p in (tmp / "Logs").glob("*"))
    assert "Primeiro plano:" in txt and "(corte:" in txt and "Evento (info): encerramento" in txt   # formato do TXT
    assert statuses == {"OK", "UNAVAILABLE"}, statuses            # a queda apareceu como indisponivel, e passou
    print(f"OK test_persistence_sqlite_and_txt (max n={n_stats})")


# ----------------------------------------------------------------------
# CLI: encerramento em todos os comandos
# ----------------------------------------------------------------------
class _Patched:
    """Faz cli.Pipeline usar uma Config apontando para o servidor local de teste."""

    def __init__(self, url, tick_raises=False, **cfg_kw):
        self.url, self.cfg_kw, self.tick_raises = url, cfg_kw, tick_raises

    def __enter__(self):
        self.orig = cli.Pipeline
        url, kw, boom = self.url, self.cfg_kw, self.tick_raises

        class Boom(Pipeline):
            def tick(self, *a, **k):
                if boom:
                    raise RuntimeError("falha no tick")
                return super().tick(*a, **k)

        def factory(cfg):
            cfg.lhm_url = url
            for k, v in kw.items():
                setattr(cfg, k, v)
            return Boom(cfg)

        cli.Pipeline = factory
        return self

    def __exit__(self, *a):
        cli.Pipeline = self.orig


def run_cli(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            code = cli.main(argv)
        except BaseException as e:      # noqa: BLE001
            code = e
    return code, buf.getvalue()


def test_cli_list_sensors_with_server_and_closed():
    base = len(lhm_threads())
    with LocalServer(sample_bytes()) as s, _Patched(s.url, lhm_prime_secs=3.0):
        t0 = time.monotonic()
        code, out = run_cli(["--list-sensors"])
        assert code == 0 and "LibreHardwareMonitor sensors detected" in out, (code, out[:300])
        assert time.monotonic() - t0 < 5.0
    assert len(lhm_threads()) == base
    # LHM com falha: o CLI continua e termina limpo, bem antes do limite de espera (5 s)
    with LocalServer(mode="http500") as s, _Patched(s.url, lhm_prime_secs=5.0, lhm_timeout_secs=0.5):
        t0 = time.monotonic()
        code, out = run_cli(["--list-sensors"])
        assert code == 2 and "UNAVAILABLE" in out and time.monotonic() - t0 < 4.0, (code, out)
    # LHM pendurado: o limite de prime encerra a espera
    with LocalServer(sample_bytes(), mode="slow") as s, _Patched(s.url, lhm_prime_secs=0.3, lhm_timeout_secs=0.4):
        t0 = time.monotonic()
        code, out = run_cli(["--list-sensors"])
        assert code == 2 and time.monotonic() - t0 < 4.0, (code, out)
    assert len(lhm_threads()) == base
    print("OK test_cli_list_sensors_with_server_and_closed")


def test_cli_monitor_apps_close_even_on_exception():
    base = len(lhm_threads())
    for argv in (["--monitor", "--minutes", "0.02"], ["--apps", "--seconds", "1"]):
        tmp = tempfile.mkdtemp(prefix="nai_cli_")
        with LocalServer(mode="http500") as s, \
                _Patched(s.url, tick_raises=True, lhm_prime_secs=0.2, lhm_timeout_secs=0.5):
            code, out = run_cli(argv + ["--root", tmp])
        assert isinstance(code, RuntimeError), (argv, code)       # a excecao propagou...
        assert len(lhm_threads()) == base, (argv, lhm_threads())  # ...e a thread NAO ficou orfa
    # execucao normal curta do --monitor
    tmp = tempfile.mkdtemp(prefix="nai_cli_")
    with LocalServer(sample_bytes()) as s, _Patched(s.url, lhm_prime_secs=3.0):
        code, out = run_cli(["--monitor", "--minutes", "0.03", "--root", tmp])
        assert code == 0 and "Encerrado" in out, (code, out[-300:])
    assert len(lhm_threads()) == base
    db = sqlite3.connect(Path(tmp) / "Data" / "notebookai.db")
    assert db.execute("select count(*) from events where type='encerramento'").fetchone()[0] == 1
    db.close()
    print("OK test_cli_monitor_apps_close_even_on_exception")


def test_cli_diagnostic_closes_thread():
    base = len(lhm_threads())
    tmp = tempfile.mkdtemp(prefix="nai_cli_")
    with LocalServer(sample_bytes()) as s, _Patched(s.url, lhm_prime_secs=3.0):
        code, out = run_cli(["--diagnostic", "--root", tmp])
    assert "LibreHardwareMonitor:\n[OK]" in out and "Sensors detected" in out, out[-1200:]
    assert len(lhm_threads()) == base
    print("OK test_cli_diagnostic_closes_thread")


if __name__ == "__main__":
    test_pipeline_wiring_and_defaults()
    test_start_once_no_duplicate_threads_and_close()
    test_context_manager_and_exception_close()
    test_tick_never_blocks_on_http()
    test_prime_bounded()
    test_readings_keys_and_metadata_match_legacy_mapping()
    test_windows_readings_untouched_and_separate()
    test_snapshot_emitted_once_not_duplicated()
    test_none_is_not_zero_and_dropped_from_sample()
    test_stale_failed_nodata_emit_nothing()
    test_failure_and_recovery_through_pipeline()
    test_persistence_sqlite_and_txt()
    test_cli_list_sensors_with_server_and_closed()
    test_cli_monitor_apps_close_even_on_exception()
    test_cli_diagnostic_closes_thread()
    print("OK test_lhm_pipeline")
# FIM test_lhm_pipeline.py