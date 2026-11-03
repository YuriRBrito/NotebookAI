"""Acesso a APIs do Windows via ctypes (sem pywin32). Em outros SO, tudo levanta Unavailable."""
import ctypes
import struct
import sys
from ctypes import wintypes

from agent.readings import Unavailable

IS_WIN = sys.platform == "win32"


def require_windows():
    if not IS_WIN:
        raise Unavailable("nao e Windows (APIs do Windows indisponiveis neste sistema)")


def is_elevated() -> bool:
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ======================================================================
# Memoria compartilhada (HWiNFO)
# ======================================================================
FILE_MAP_READ = 0x0004


def open_shared_memory(name: str) -> tuple[int, int]:
    """Abre mapping nomeado existente (somente leitura). Retorna (handle, ponteiro)."""
    require_windows()
    k32 = ctypes.windll.kernel32
    k32.OpenFileMappingW.restype = wintypes.HANDLE
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, ctypes.c_size_t]
    h = k32.OpenFileMappingW(FILE_MAP_READ, False, name)
    if not h:
        err = ctypes.GetLastError()
        if err == 2:
            raise Unavailable(f"memoria compartilhada '{name}' nao encontrada "
                              "(HWiNFO fechado ou 'Shared Memory Support' desligado)")
        if err == 5:
            raise Unavailable(f"acesso negado a '{name}' (erro 5): HWiNFO pode estar elevado "
                              "e este processo nao")
        raise Unavailable(f"OpenFileMapping falhou (erro {err})")
    p = k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
    if not p:
        err = ctypes.GetLastError()
        k32.CloseHandle(h)
        raise Unavailable(f"MapViewOfFile falhou (erro {err})")
    return h, p


def close_shared_memory(h, p):
    if not IS_WIN:
        return
    k32 = ctypes.windll.kernel32
    k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    if p:
        k32.UnmapViewOfFile(p)
    if h:
        k32.CloseHandle(h)


# ======================================================================
# Memoria: GetPerformanceInfo
# ======================================================================
class _PERF(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize",
        "HandleCount", "ProcessCount", "ThreadCount")]


def performance_info() -> dict:
    require_windows()
    pi = _PERF()
    pi.cb = ctypes.sizeof(pi)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        raise OSError("GetPerformanceInfo falhou")
    ps = pi.PageSize
    return {"commit_total": pi.CommitTotal * ps, "commit_limit": pi.CommitLimit * ps,
            "phys_total": pi.PhysicalTotal * ps, "phys_avail": pi.PhysicalAvailable * ps,
            "system_cache": pi.SystemCache * ps, "processes": pi.ProcessCount,
            "threads": pi.ThreadCount, "handles": pi.HandleCount}


# ======================================================================
# Energia: GetSystemPowerStatus
# ======================================================================
class _SPS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def system_power_status() -> dict:
    require_windows()
    s = _SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        raise OSError("GetSystemPowerStatus falhou")
    return {"ac": {0: False, 1: True}.get(s.ACLineStatus),          # 255 = desconhecido -> None
            "battery_pct": None if s.BatteryLifePercent == 255 else s.BatteryLifePercent,
            "flag": s.BatteryFlag,
            "life_secs": None if s.BatteryLifeTime == 0xFFFFFFFF else s.BatteryLifeTime,
            "battery_saver": bool(s.SystemStatusFlag & 1)}


# ======================================================================
# Bateria: IOCTL (SetupAPI + DeviceIoControl)
# ======================================================================
class _GUID(ctypes.Structure):
    _fields_ = [("D1", wintypes.DWORD), ("D2", wintypes.WORD), ("D3", wintypes.WORD),
                ("D4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    import uuid
    u = uuid.UUID(s)
    g = _GUID()
    g.D1, g.D2, g.D3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.D4[i] = b
    return g


class _SPDID(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("Guid", _GUID), ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


IOCTL_BATTERY_QUERY_TAG = 0x294040
IOCTL_BATTERY_QUERY_INFORMATION = 0x294044
IOCTL_BATTERY_QUERY_STATUS = 0x29404C
BATTERY_UNKNOWN_TIME = 0xFFFFFFFF
INVALID_HANDLE = ctypes.c_void_p(-1).value


class BatteryDevice:
    """Handle aberto para o 1o dispositivo de bateria. Reabrir se uma chamada falhar."""

    def __init__(self):
        require_windows()
        self.handle = None
        self.tag = 0
        self.count = 0
        self._open()

    def _open(self):
        sa = ctypes.windll.setupapi
        k32 = ctypes.windll.kernel32
        sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR,
                                            wintypes.HWND, wintypes.DWORD]
        sa.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.POINTER(_GUID), wintypes.DWORD,
                                                   ctypes.POINTER(_SPDID)]
        sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SPDID),
                                                        ctypes.c_void_p, wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        guid = _guid("72631e54-78a4-11d0-bcf7-00aa00b7b32a")  # GUID_DEVCLASS_BATTERY
        hdev = sa.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)  # PRESENT|DEVICEINTERFACE
        if not hdev or hdev == INVALID_HANDLE:
            raise Unavailable(f"SetupDiGetClassDevs falhou (erro {ctypes.GetLastError()})")
        try:
            paths = []
            i = 0
            while True:
                did = _SPDID()
                did.cbSize = ctypes.sizeof(did)
                if not sa.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), i, ctypes.byref(did)):
                    break
                req = wintypes.DWORD(0)
                sa.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0, ctypes.byref(req), None)
                buf = ctypes.create_string_buffer(req.value)
                struct.pack_into("I", buf, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
                if sa.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf, req.value, None, None):
                    paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
                i += 1
        finally:
            sa.SetupDiDestroyDeviceInfoList(ctypes.c_void_p(hdev))
        self.count = len(paths)
        if not paths:
            raise Unavailable("nenhuma bateria encontrada (SetupAPI nao listou dispositivos)")
        h = k32.CreateFileW(paths[0], 0xC0000000, 3, None, 3, 0x80, None)
        if not h or h == INVALID_HANDLE:
            raise Unavailable(f"CreateFile na bateria falhou (erro {ctypes.GetLastError()})")
        self.handle = h
        wait = wintypes.DWORD(0)
        tag = wintypes.DWORD(0)
        ret = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(ctypes.c_void_p(h), IOCTL_BATTERY_QUERY_TAG, ctypes.byref(wait), 4,
                                 ctypes.byref(tag), 4, ctypes.byref(ret), None)
        if not ok or tag.value == 0:
            raise Unavailable(f"IOCTL_BATTERY_QUERY_TAG sem tag (erro {ctypes.GetLastError()}): "
                              "bateria ausente?")
        self.tag = tag.value

    def _ioctl(self, code, inbuf: bytes, outsize: int) -> bytes:
        k32 = ctypes.windll.kernel32
        out = ctypes.create_string_buffer(outsize)
        ret = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(ctypes.c_void_p(self.handle), code, inbuf, len(inbuf), out, outsize,
                                 ctypes.byref(ret), None)
        if not ok:
            raise OSError(ctypes.GetLastError(), "DeviceIoControl falhou")
        return out.raw[:ret.value]

    def _query_info(self, level: int, at_rate: int = 0, outsize: int = 256) -> bytes:
        return self._ioctl(IOCTL_BATTERY_QUERY_INFORMATION,
                           struct.pack("<IIi", self.tag, level, at_rate), outsize)

    def information(self) -> dict:
        raw = self._query_info(0, outsize=40)
        cap, tech, chem, design, full, a1, a2, bias, cycles = struct.unpack("<IB3x4s6I", raw[:40])
        return {"capabilities": cap, "technology": tech, "chemistry": chem.decode("ascii", "replace"),
                "design_mwh": design, "full_mwh": full, "cycles": cycles,
                "relative_capacity": bool(cap & 0x40000000)}

    def status(self) -> dict:
        raw = self._ioctl(IOCTL_BATTERY_QUERY_STATUS, struct.pack("<IIIII", self.tag, 0, 0, 0, 0), 16)
        power, cap, volt, rate = struct.unpack("<IIIi", raw[:16])
        return {"power_state": power, "remaining_mwh": None if cap == 0xFFFFFFFF else cap,
                "voltage_mv": None if volt == 0xFFFFFFFF else volt,
                "rate_mw": None if rate == -0x80000000 else rate,
                "online": bool(power & 1), "discharging": bool(power & 2),
                "charging": bool(power & 4), "critical": bool(power & 8)}

    def estimated_time_secs(self):
        raw = self._query_info(3, 0, 4)
        v = struct.unpack("<I", raw[:4])[0]
        return None if v == BATTERY_UNKNOWN_TIME else v

    def text(self, level: int) -> str | None:
        try:
            raw = self._query_info(level, 0, 256)
        except OSError:
            return None
        return raw.decode("utf-16-le", "replace").rstrip("\x00") or None

    def close(self):
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


# ======================================================================
# Janelas / processos
# ======================================================================
def foreground_window() -> dict | None:
    require_windows()
    u32 = ctypes.windll.user32
    u32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    n = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return {"hwnd": int(hwnd), "pid": pid.value, "title": buf.value, "minimized": bool(u32.IsIconic(hwnd))}


def top_level_windows() -> dict[int, dict]:
    """pid -> {'visible': n janelas visiveis, 'minimized': n minimizadas} (janelas com titulo)."""
    require_windows()
    u32 = ctypes.windll.user32
    result: dict[int, dict] = {}
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if u32.IsWindowVisible(hwnd) and u32.GetWindowTextLengthW(hwnd) > 0:
            # ignora janelas "tool" (sem botao na barra de tarefas)
            ex = u32.GetWindowLongW(hwnd, -20)
            if ex & 0x80:           # WS_EX_TOOLWINDOW
                return True
            pid = wintypes.DWORD(0)
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            d = result.setdefault(pid.value, {"visible": 0, "minimized": 0})
            d["visible"] += 1
            if u32.IsIconic(hwnd):
                d["minimized"] += 1
        return True

    u32.EnumWindows(CB(cb), 0)
    return result


def process_image_path(pid: int) -> str | None:
    """Caminho do exe com PROCESS_QUERY_LIMITED_INFORMATION (funciona em mais casos que psutil)."""
    require_windows()
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


_FRIENDLY_CACHE: dict[str, str | None] = {}


def file_description(path: str) -> str | None:
    """FileDescription do recurso de versao do exe (nome amigavel)."""
    if path in _FRIENDLY_CACHE:
        return _FRIENDLY_CACHE[path]
    out = None
    try:
        require_windows()
        v = ctypes.windll.version
        size = v.GetFileVersionInfoSizeW(path, None)
        if size:
            data = ctypes.create_string_buffer(size)
            if v.GetFileVersionInfoW(path, 0, size, data):
                ptr = ctypes.c_void_p()
                ln = wintypes.UINT()
                if v.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(ln)) and ln.value >= 4:
                    lang, cp = struct.unpack("<HH", ctypes.string_at(ptr, 4))
                    sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
                    if v.VerQueryValueW(data, sub, ctypes.byref(ptr), ctypes.byref(ln)) and ln.value:
                        out = ctypes.wstring_at(ptr).strip() or None
    except Exception:
        out = None
    _FRIENDLY_CACHE[path] = out
    return out


# ======================================================================
# PDH (contadores de desempenho do Windows, nomes em ingles)
# ======================================================================
class _PDHVAL(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("v", ctypes.c_double)]


class _PDHITEM(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_void_p), ("CStatus", wintypes.DWORD), ("v", ctypes.c_double)]


PDH_FMT_DOUBLE = 0x200
PDH_MORE_DATA = 0x800007D2


class PDHQuery:
    def __init__(self):
        require_windows()
        self.pdh = ctypes.windll.pdh
        self.q = ctypes.c_void_p()
        rc = self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q))
        if rc != 0:
            raise OSError(f"PdhOpenQuery falhou (0x{rc & 0xFFFFFFFF:08X})")
        self.counters: dict[str, ctypes.c_void_p] = {}
        self.errors: dict[str, str] = {}

    def add(self, path: str) -> bool:
        h = ctypes.c_void_p()
        rc = self.pdh.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h))
        if rc != 0:
            self.errors[path] = f"0x{rc & 0xFFFFFFFF:08X}"
            return False
        self.counters[path] = h
        return True

    def collect(self):
        self.pdh.PdhCollectQueryData(self.q)

    def value(self, path: str) -> float | None:
        h = self.counters.get(path)
        if h is None:
            return None
        v = _PDHVAL()
        rc = self.pdh.PdhGetFormattedCounterValue(h, PDH_FMT_DOUBLE, None, ctypes.byref(v))
        if rc != 0 or v.CStatus not in (0, 1):
            return None
        return v.v

    def array(self, path: str) -> dict[str, float] | None:
        h = self.counters.get(path)
        if h is None:
            return None
        size = wintypes.DWORD(0)
        cnt = wintypes.DWORD(0)
        rc = self.pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(cnt), None)
        if (rc & 0xFFFFFFFF) != PDH_MORE_DATA:
            return None
        buf = ctypes.create_string_buffer(size.value)
        rc = self.pdh.PdhGetFormattedCounterArrayW(h, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(cnt), buf)
        if rc != 0:
            return None
        items = ctypes.cast(buf, ctypes.POINTER(_PDHITEM))
        out = {}
        for i in range(cnt.value):
            it = items[i]
            if it.CStatus in (0, 1) and it.szName:
                out[ctypes.wstring_at(it.szName)] = it.v
        return out

    def close(self):
        if self.q:
            self.pdh.PdhCloseQuery(self.q)
            self.q = ctypes.c_void_p()
