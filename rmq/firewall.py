"""Whether Windows Firewall lets other PCs of the network reach the manager, and the one rule that fixes it.

The manager listens on its network port as pythonw.exe. Windows normally asks once whether to allow that; if the
question was missed (a game in full screen) or answered "no", nothing can connect and the other PC only sees
"нет связи". The check reads the firewall's rules through its own COM object (no administrator rights, the same in
every Windows language). The fix adds one narrow inbound rule - TCP on the manager's port, for this program only,
on the network type Tailscale is in - and needs the owner's consent (UAC).
"""
from __future__ import annotations

import base64
import ctypes
import json
import subprocess
from ctypes import wintypes

RULE = "RMQ resource manager (network)"
_PROFILE_BIT = {"DomainAuthenticated": 1, "Private": 2, "Public": 4}
_PROFILE_NAME = {1: "domain", 2: "private", 4: "public"}

_CHECK = r"""
$ErrorActionPreference = 'Stop'
$fw = New-Object -ComObject HNetCfg.FwPolicy2
$exe = '{exe}'
$port = '{port}'
$cat = $null
try {{ $cat = [string](Get-NetConnectionProfile -InterfaceAlias '{alias}' -ErrorAction Stop | Select-Object -First 1).NetworkCategory }} catch {{ }}
if (-not $cat) {{ $cat = 'Private' }}
$bit = 4; if ($cat -eq 'Private') {{ $bit = 2 }} elseif ($cat -eq 'DomainAuthenticated') {{ $bit = 1 }}
$rules = @($fw.Rules | Where-Object {{ $_.Direction -eq 1 -and $_.Enabled -and $_.ApplicationName -and
    ($_.ApplicationName -ieq $exe) -and (($_.Profiles -band $bit) -ne 0) -and ($_.Protocol -eq 6 -or $_.Protocol -eq 256) }})
$allow = @($rules | Where-Object {{ $_.Action -eq 1 -and ($_.LocalPorts -eq '*' -or (($_.LocalPorts -split ',') -contains $port)) }}).Count
$block = @($rules | Where-Object {{ $_.Action -eq 0 }}).Count
@{{ category = $cat; enabled = [bool]$fw.FirewallEnabled($bit); inbound = [int]$fw.DefaultInboundAction($bit);
    allow = $allow; block = $block }} | ConvertTo-Json -Compress
"""


def state_from(r: dict) -> str:
    """ok: other PCs can connect; blocked: a rule forbids it; missing: nothing allows it (and inbound is blocked)."""
    if not r.get("enabled"):
        return "ok"           # the firewall is off for this network type
    if r.get("block"):
        return "blocked"      # a block rule beats any allow rule
    if r.get("allow") or r.get("inbound") == 1:
        return "ok"
    return "missing"


def check(exe: str, port: int, alias: str = "Tailscale") -> dict:
    """{"state", "category", ...}; state "unknown" when the check itself fails."""
    script = _CHECK.format(exe=exe.replace("'", "''"), port=int(port), alias=alias.replace("'", "''"))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=60,
                           creationflags=0x08000000)
        data = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        data = None
    if not isinstance(data, dict):
        return {"state": "unknown", "category": None}
    return {"state": state_from(data), "category": data.get("category"), "rule": RULE}


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


_ALLOW = r"""
$ErrorActionPreference = 'Stop'
try {{
  $exe = '{exe}'
  # a rule that forbids this program beats any rule that allows it (Windows makes one when its question is cancelled)
  Get-NetFirewallApplicationFilter | Where-Object {{ $_.Program -ieq $exe }} | Get-NetFirewallRule |
    Where-Object {{ $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' }} | Remove-NetFirewallRule
  Get-NetFirewallRule -DisplayName '{rule}' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName '{rule}' -Direction Inbound -Action Allow -Program $exe -Protocol TCP `
    -LocalPort {port} -Profile {profile} -Description 'RMQ: other PCs of the compute network connect to the manager' | Out-Null
  exit 0
}} catch {{ exit 1 }}
"""


def allow(exe: str, port: int, category: str | None) -> tuple[bool, str]:
    """Remove what blocks this program and add the one narrow rule, asking the owner through UAC.
    (done, a message for the dashboard)."""
    profile = _PROFILE_NAME.get(_PROFILE_BIT.get(category or "Private", 2), "private").capitalize()
    script = _ALLOW.format(exe=exe.replace("'", "''"), rule=RULE, port=int(port), profile=profile)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile, info.nShow = "runas", "powershell.exe", 0
    info.lpParameters = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        return False, "вы не разрешили изменение в окне Windows" if err == 1223 else f"Windows не дала это сделать (ошибка {err})"
    try:
        kernel32.WaitForSingleObject(info.hProcess, 120_000)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    finally:
        kernel32.CloseHandle(info.hProcess)
    if code.value != 0:
        return False, f"правило не добавилось (код {code.value})"
    return True, f"готово: брандмауэр пропускает подключения других ПК к менеджеру (порт {int(port)}, сеть «{category or 'Private'}»)"
