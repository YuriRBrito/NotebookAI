"""Informacoes estaticas (modelo de SSD, memoria fisica, pagefile, GPU) via CIM/PowerShell.
Executa uma vez e guarda em cache. Fonte: 'Windows CIM (PowerShell)'."""
import json
import subprocess

from agent import winapi
from agent.readings import Unavailable

SRC = "Windows CIM (PowerShell)"
_CACHE: dict | None = None
_ERR: str | None = None

PS = r"""
$ErrorActionPreference='SilentlyContinue'
$o=@{}
$o.cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed)
$o.mem=@(Get-CimInstance Win32_PhysicalMemory | Select-Object Manufacturer,PartNumber,Capacity,Speed,ConfiguredClockSpeed,SMBIOSMemoryType)
$o.pagefile=@(Get-CimInstance Win32_PageFileUsage | Select-Object Name,AllocatedBaseSize,CurrentUsage)
$o.disks=@(Get-PhysicalDisk | Select-Object FriendlyName,MediaType,BusType,Size,HealthStatus,OperationalStatus)
$o.gpu=@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion)
$o.os=@(Get-CimInstance Win32_OperatingSystem | Select-Object Caption,BuildNumber,Version)
$o | ConvertTo-Json -Depth 4 -Compress
"""


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def get(force: bool = False) -> dict:
    global _CACHE, _ERR
    if _CACHE is not None and not force:
        return _CACHE
    if _ERR and not force:
        raise Unavailable(_ERR)
    winapi.require_windows()
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
            capture_output=True, text=True, timeout=40, creationflags=0x08000000)
    except FileNotFoundError:
        _ERR = "powershell.exe nao encontrado"
        raise Unavailable(_ERR)
    except subprocess.TimeoutExpired:
        _ERR = "PowerShell excedeu 40 s coletando informacoes estaticas"
        raise Unavailable(_ERR)
    if cp.returncode != 0 or not cp.stdout.strip():
        _ERR = f"PowerShell retornou {cp.returncode}: {cp.stderr.strip()[:200]}"
        raise Unavailable(_ERR)
    raw = json.loads(cp.stdout)
    _CACHE = {k: _as_list(v) for k, v in raw.items()}
    return _CACHE
