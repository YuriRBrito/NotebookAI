"""HWiNFO Shared Memory (v2). Descoberta dinamica; nada depende de nomes fixos.

ATENCAO: o layout abaixo vem do SDK publico do HWiNFO (hwisenssm2.h) conforme meu
conhecimento. Nao pude validar contra um HWiNFO real. O parser confere tamanhos e
offsets e falha com mensagem clara se algo nao bater.
"""
import re
import struct

from agent import winapi
from agent.normalize import map_hwinfo
from agent.readings import Collector, CollectorResult, Reading, Unavailable

SHM_NAME = "Global\\HWiNFO_SENS_SM2"
SIG_ACTIVE = 0x53695748      # 'HWiS'
SIG_DEAD = 0x44414544        # 'DEAD'
HEADER_FMT = "<IIIqIIIIII"   # sig, ver, rev, poll_time, offS, szS, nS, offR, szR, nR
HEADER_SIZE = struct.calcsize(HEADER_FMT)   # 44
SENSOR_MIN = 4 + 4 + 128 + 128              # 264
READING_MIN = 4 + 4 + 4 + 128 + 128 + 16 + 4 * 8   # 316
TYPES = {0: "none", 1: "temp", 2: "volt", 3: "fan", 4: "current", 5: "power",
         6: "clock", 7: "usage", 8: "other"}


def _cstr(b: bytes) -> str:
    b = b.split(b"\x00", 1)[0]
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def parse_header(buf: bytes) -> dict:
    if len(buf) < HEADER_SIZE:
        raise ValueError("buffer menor que o cabecalho")
    sig, ver, rev, poll, offS, szS, nS, offR, szR, nR = struct.unpack_from(HEADER_FMT, buf, 0)
    if sig == SIG_DEAD:
        raise Unavailable("HWiNFO sinalizou memoria compartilhada encerrada ('DEAD')")
    if sig != SIG_ACTIVE:
        raise ValueError(f"assinatura invalida 0x{sig:08X} (esperado 'HWiS')")
    if szS < SENSOR_MIN or szR < READING_MIN:
        raise ValueError(f"tamanho de elemento menor que o esperado (sensor {szS}<{SENSOR_MIN} "
                         f"ou leitura {szR}<{READING_MIN}); layout diferente do assumido")
    if nS > 5000 or nR > 20000:
        raise ValueError(f"contagem absurda de sensores/leituras ({nS}/{nR})")
    return {"version": ver, "revision": rev, "poll_time": poll, "offS": offS, "szS": szS, "nS": nS,
            "offR": offR, "szR": szR, "nR": nR,
            "total": max(offS + szS * nS, offR + szR * nR)}


def parse_buffer(buf: bytes) -> tuple[dict, list[dict], list[dict]]:
    h = parse_header(buf)
    if len(buf) < h["total"]:
        raise ValueError(f"buffer ({len(buf)}) menor que o necessario ({h['total']})")
    sensors = []
    for i in range(h["nS"]):
        o = h["offS"] + i * h["szS"]
        sid, inst = struct.unpack_from("<II", buf, o)
        orig = _cstr(buf[o + 8:o + 136])
        user = _cstr(buf[o + 136:o + 264])
        sensors.append({"id": sid, "inst": inst, "name": user or orig, "name_orig": orig})
    readings = []
    for i in range(h["nR"]):
        o = h["offR"] + i * h["szR"]
        t, sidx, rid = struct.unpack_from("<III", buf, o)
        lab_o = _cstr(buf[o + 12:o + 140])
        lab_u = _cstr(buf[o + 140:o + 268])
        unit = _cstr(buf[o + 268:o + 284])
        val, vmin, vmax, vavg = struct.unpack_from("<4d", buf, o + 284)
        readings.append({"type": TYPES.get(t, f"type{t}"), "sensor_index": sidx, "reading_id": rid,
                         "label": lab_u or lab_o, "label_orig": lab_o, "unit": unit,
                         "value": val, "min": vmin, "max": vmax, "avg": vavg})
    return h, sensors, readings


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


class HwinfoCollector(Collector):
    name = "HWiNFO"
    admin_expected = "provavelmente nao para LER (o HWiNFO pode estar elevado); a verificar"

    def __init__(self):
        self.h = self.p = None
        self.last_poll = None
        self.discovery: dict = {}

    def _read_raw(self) -> bytes:
        import ctypes
        if self.p is None:
            self.h, self.p = winapi.open_shared_memory(SHM_NAME)
        head = ctypes.string_at(self.p, HEADER_SIZE)
        total = parse_header(head)["total"]
        return ctypes.string_at(self.p, total)

    def read_buffer(self, buf: bytes) -> CollectorResult:
        """Separado de collect() para permitir teste com buffer sintetico."""
        h, sensors, readings = parse_buffer(buf)
        res = CollectorResult(self.name)
        note = ""
        if self.last_poll is not None and h["poll_time"] == self.last_poll:
            note = "poll_time nao mudou desde a ultima leitura (HWiNFO congelado?)"
        self.last_poll = h["poll_time"]
        out: list[Reading] = []
        for r in readings:
            s = sensors[r["sensor_index"]] if r["sensor_index"] < len(sensors) else {"name": "?", "id": 0, "inst": 0}
            out.append(Reading(
                key="", value=r["value"], unit=r["unit"], source="HWiNFO", label=r["label"],
                vmin=r["min"], vmax=r["max"], vavg=r["avg"],
                sensor_id=f"{s['id']}:{s['inst']}:{r['reading_id']}",
                category=s["name"], group=s["name"],   # categoria real definida no mapeamento
                note=r["type"]))
        mapped = map_hwinfo(out, [s["name"] for s in sensors])
        res.readings = mapped
        res.data = {"version": f"{h['version']}.{h['revision']}", "poll_time": h["poll_time"],
                    "n_sensors": len(sensors), "n_readings": len(readings), "stale_note": note}
        if note:
            res.status, res.reason = "OK", note
        return res

    def collect(self) -> CollectorResult:
        winapi.require_windows()
        try:
            buf = self._read_raw()
        except Unavailable:
            self._reset()
            raise
        except ValueError as e:
            self._reset()
            raise Unavailable(f"layout/assinatura inesperados: {e}")
        return self.read_buffer(buf)

    def _reset(self):
        winapi.close_shared_memory(self.h, self.p)
        self.h = self.p = None
