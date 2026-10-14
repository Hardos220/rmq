"""Why another PC of the network cannot be reached, in words the owner can act on.

Asks Tailscale on this PC (`tailscale status --json`: is it connected, does it see that PC, has the tunnel to it
shaken hands lately) and Windows (which adapter the route to that PC goes through, which VPN holds the default
route), instead of a bare "нет связи". Runs only while some PC is unreachable, about once a minute, without
administrator rights and without windows.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

NO_WINDOW = 0x08000000
HANDSHAKE_STALE_S = 180  # WireGuard renews its session every 2 minutes while traffic flows
TS_RANGE = "100.64.0.0/10"


def tailscale_exe() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        p = Path(base) / "Tailscale" / "tailscale.exe" if base else None
        if p and p.exists():
            return str(p)
    return None


def tailscale_status() -> dict | None:
    """Tailscale's own view; None without Tailscale's command line, {} when its service does not answer."""
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=15, creationflags=NO_WINDOW)
        out = r.stdout.decode("utf-8", errors="replace").strip()
        return json.loads(out) if out.startswith("{") else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


_ROUTES = r"""
$ErrorActionPreference = 'SilentlyContinue'
$via = @{{}}
foreach ($ip in @({ips})) {{
  $r = Find-NetRoute -RemoteIPAddress $ip | Where-Object {{ $_.DestinationPrefix }} | Select-Object -First 1
  $via[$ip] = [string]$r.InterfaceAlias
}}
$hw = @{{}}
Get-NetAdapter | ForEach-Object {{ $hw[$_.Name] = [bool]$_.HardwareInterface }}
$vpn = @(Get-NetRoute -AddressFamily IPv4 | Where-Object {{ $_.DestinationPrefix -in '0.0.0.0/0','0.0.0.0/1','128.0.0.0/1' }} |
  ForEach-Object {{ $_.InterfaceAlias }} | Sort-Object -Unique |
  Where-Object {{ $_ -notmatch 'tailscale' -and $hw.ContainsKey($_) -and -not $hw[$_] }})
@{{ via = $via; vpn = $vpn }} | ConvertTo-Json -Compress
"""


def routes(ips: list[str]) -> tuple[dict[str, str], list[str]]:
    """({that PC's Tailscale IP: the adapter the route to it goes through}, the VPNs that hold the default route:
    virtual adapters other than Tailscale)."""
    ips = [ip for ip in ips if re.fullmatch(r"[\d.]+", ip)]
    script = _ROUTES.format(ips=",".join(f"'{ip}'" for ip in ips) or "''")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60, creationflags=NO_WINDOW)
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}, []
    vpn = data.get("vpn") or []
    return {k: v for k, v in (data.get("via") or {}).items() if v}, [vpn] if isinstance(vpn, str) else list(vpn)


def _when(text: str | None) -> float | None:
    """A Go time ("2026-10-14T10:37:12.123456789+03:00"; the zero time means never) as a Unix time."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|([+-])(\d\d):(\d\d))?", text or "")
    if not m or int(m[1]) < 2000:
        return None
    tz = timezone.utc
    if m[8]:
        off = timedelta(hours=int(m[9]), minutes=int(m[10]))
        tz = timezone(off if m[8] == "+" else -off)
    return datetime(*(int(m[i]) for i in range(1, 7)), tzinfo=tz).timestamp()


def paths_from(ts: dict | None) -> dict[str, dict]:
    """{Tailscale IP of a peer: {"direct": connected directly, "relay": the code of the relay server it goes through
    otherwise}} from tailscale_status()."""
    out = {}
    for p in ((ts or {}).get("Peer") or {}).values():
        d = {"direct": bool(p.get("CurAddr")), "relay": p.get("Relay") or None}
        for ip in p.get("TailscaleIPs") or []:
            out[ip] = d
    return out


def _names(vpns: list[str]) -> str:
    return ", ".join(f"«{v}»" for v in vpns)


def diagnose(peer_ip: str, ts: dict | None, via: str | None, vpns: list[str], now: float | None = None) -> dict | None:
    """{"code", "title", "hint"}: why that PC (its Tailscale IP) cannot be reached and what to do; None when
    nothing here explains it. `ts` is tailscale_status(), `via` the adapter the route to that PC goes through."""
    now = now or time.time()
    vpn_hint = (f" На этом ПК включён VPN {_names(vpns)} — он может мешать Tailscale. Если переподключение не поможет, "
                f"исключите Tailscale из VPN (раздельное туннелирование: адреса {TS_RANGE} и, если VPN умеет исключать "
                "программы, tailscaled.exe) или проверьте связь с выключенным VPN.") if vpns else ""
    peer = None
    if ts is not None:
        if ts.get("BackendState") != "Running":
            return {"code": "ts_off", "title": "Tailscale на этом ПК не подключён",
                    "hint": "Откройте Tailscale (значок в трее) и нажмите Connect; если попросит — войдите в свой аккаунт."}
        if (ts.get("Self") or {}).get("Online") is False:
            return {"code": "ts_cut", "title": "Tailscale на этом ПК не может связаться со своими серверами",
                    "hint": "Проверьте интернет и переподключите Tailscale (значок в трее → Disconnect → Connect)."
                    + (f" Если включён VPN {_names(vpns)}, он может не пускать Tailscale (например, функция «блокировать "
                       "трафик вне VPN»): исключите из него tailscaled.exe или выключите VPN на время." if vpns else "")}
        peer = next((p for p in (ts.get("Peer") or {}).values() if peer_ip in (p.get("TailscaleIPs") or [])), None)
        if peer is None:
            return {"code": "peer_unknown", "title": "Tailscale не видит тот ПК",
                    "hint": "Его нет в вашей сети Tailscale: он в другом аккаунте и с вами не поделился (Share в "
                            "login.tailscale.com/admin/machines) или удалён из Tailscale."}
        if not peer.get("Online"):
            return {"code": "peer_offline", "title": "Тот ПК не подключён к Tailscale",
                    "hint": "Он выключен или спит, либо Tailscale на нём закрыт или отключён (на нём: значок "
                            "Tailscale в трее → Connect)."}
    if via and "tailscale" not in via.lower():
        return {"code": "vpn_route", "title": f"Трафик к тому ПК уходит в «{via}», а не в Tailscale",
                "hint": f"Добавьте в «{via}» исключение (раздельное туннелирование) для адресов {TS_RANGE} "
                        "или выключите его на время."}
    if peer is None:
        return None
    shook = _when(peer.get("LastHandshake"))
    if shook is None or now - shook > HANDSHAKE_STALE_S:
        return {"code": "tunnel", "title": "Tailscale видит тот ПК, но туннель между ПК не устанавливается",
                "hint": "Переподключите Tailscale: сначала на том ПК, потом на этом (значок в трее → Disconnect → "
                        "Connect)." + vpn_hint}
    return {"code": "manager", "title": "Tailscale до того ПК работает, а менеджер на нём не отвечает",
            "hint": "Менеджер там не запущен, в режиме «Офлайн» или его не пускает брандмауэр Windows на том ПК (там "
                    "на вкладке «Сеть» будет кнопка «Разрешить в брандмауэре»)."}


def is_tailscale(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(TS_RANGE)
    except ValueError:
        return False


def check(peer_ips: list[str]) -> tuple[dict[str, dict], list[str]]:
    """({IP: diagnose(...)} for the IPs it explains, the VPNs on this PC). Only Tailscale addresses are looked at:
    a PC reached some other way (a test on 127.0.0.1, a LAN address set by hand) is not Tailscale's business."""
    peer_ips = [ip for ip in peer_ips if is_tailscale(ip)]
    if not peer_ips:
        return {}, []
    ts = tailscale_status()
    via, vpns = routes(peer_ips)
    out = {}
    for ip in peer_ips:
        d = diagnose(ip, ts, via.get(ip), vpns)
        if d:
            out[ip] = d
    return out, vpns
