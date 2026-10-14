"""Component temperatures without drivers or admin rights, and the overheating rule.

Sensors (documented, read-only):
- Board: ACPI thermal zones from Windows performance counters, plus the firmware's own "passive
  cooling" flag (it is throttling the CPU because of heat). On many laptops the zone is a board
  sensor rather than the CPU die: the die temperature needs a kernel driver to read.
- GPU: NVIDIA's NVML library that ships with the driver. It is queried only while the GPU is
  awake (busy, or still warm), because polling a sleeping laptop GPU would wake it up and heat it.
- SSD: the drive's own temperature and its limits via the standard storage property query.
- CPU die: LibreHardwareMonitor's local web server, when it is installed (tools/install_cpu_sensors.ps1)
  or the owner runs it from any folder with its web server on: it reads the CPU sensors through its
  driver and runs with administrator rights, so the manager itself does not need them.

HeatRegulator keeps every component at or below its target temperature by smoothly scaling the CPU
the tasks get. ThermalGuard is the safety net behind it: a component past its hot limit freezes every
task until all components are back to normal and stay there for a while.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import struct
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import winapi


@dataclass
class Reading:
    key: str                 # cpu | board | gpu | ssd
    label: str
    temp: float | None       # degrees C; None = not measured right now (asleep or unavailable)
    target: float            # the regulator keeps the component at or below this
    hot: float               # held for hot_confirm_s: every task is frozen
    normal: float            # frozen tasks go on once every component is back at or below this
    throttling: bool = False  # the hardware itself reports thermal throttling
    note: str = ""


def readings_from(cfg: dict, board: tuple, gpu: tuple, ssd: tuple, cpu: float | None = None) -> list[Reading]:
    out = []
    if cpu is not None:
        out.append(Reading("cpu", "Процессор", cpu, cfg["cpu_target_c"], cfg["cpu_hot_c"], cfg["cpu_normal_c"]))
    if board[0] is not None or board[1]:
        out.append(Reading("board", "Плата (ACPI)", board[0], cfg["board_target_c"], cfg["board_hot_c"],
                           cfg["board_normal_c"], board[1]))
    if gpu[2]:  # an NVIDIA GPU exists
        out.append(Reading("gpu", "Видеокарта", gpu[0], cfg["gpu_target_c"], cfg["gpu_hot_c"], cfg["gpu_normal_c"],
                           gpu[1], "" if gpu[0] is not None else "спит"))
    if ssd[0] is not None:
        hot = min(cfg["ssd_hot_c"], ssd[1] - 3) if ssd[1] else cfg["ssd_hot_c"]
        out.append(Reading("ssd", "SSD", ssd[0], min(cfg["ssd_target_c"], hot - 5), hot, cfg["ssd_normal_c"]))
    return out


class ThermalGuard:
    """normal -> hot after `hot_confirm_s` above a hot limit (or of hardware throttling); hot -> normal
    only after every sensor has been at or below its normal temperature for `cool_confirm_s`."""

    def __init__(self):
        self.state = "normal"
        self.trigger = ""            # what caused the freeze, for the dashboard
        self.cool_left: float | None = None
        self._hot_since = self._normal_since = None

    def update(self, now: float, readings: list[Reading], cfg: dict) -> str:
        if not cfg["enabled"]:
            self.state, self.trigger, self.cool_left = "normal", "", None
            return self.state
        hot = [r for r in readings if r.temp is not None and r.temp >= r.hot]
        if cfg["freeze_on_throttling"]:
            hot += [r for r in readings if r.throttling and r not in hot]
        normal = all((r.temp is None or r.temp <= r.normal) and not r.throttling for r in readings)

        if self.state != "hot":
            if not hot:
                self._hot_since = None
                return self.state
            self._hot_since = self._hot_since if self._hot_since is not None else now
            if now - self._hot_since < cfg["hot_confirm_s"]:
                return self.state
            r = hot[0]
            self.trigger = (f"{r.label}: {r.temp:.0f}°C (порог {r.hot:.0f}°C)" if r.temp is not None
                            and r.temp >= r.hot else "BIOS этого ПК сам тормозит процессор из-за нагрева")
            self.state, self._normal_since = "hot", None

        if normal:
            self._normal_since = self._normal_since if self._normal_since is not None else now
            self.cool_left = max(0.0, cfg["cool_confirm_s"] - (now - self._normal_since))
            if self.cool_left <= 0:
                self.state, self.trigger, self.cool_left, self._hot_since = "normal", "", None, None
        else:
            self._normal_since, self.cool_left = None, None
        return self.state


class HeatRegulator:
    """Keeps every component at or below its target by smoothly scaling the CPU share of the tasks.

    One integral loop per component, and the lowest share wins. A loop takes CPU away while its component
    is above the target and not cooling down yet, holds still within BAND degrees below the target, and
    gives CPU back slowly once the component is cooler than that and not heating up. The CPU die answers
    to the load within seconds; the board and the SSD answer slowly, and the GPU is heated mostly by its own
    work, so their loops are slower, and the GPU and the SSD may cut the tasks' CPU at most in half.
    """

    BAND = 2.0
    # averaging, trend window (s); CPU taken per °C above the target per s; given back per °C below the
    # band per s; the fastest give-back per s; °C/s that count as "cooling down" and as "heating up"
    FAST = (5.0, 20.0, 0.010, 0.004, 0.020, -0.02, 0.05)
    SLOW = (20.0, 90.0, 0.002, 0.001, 0.005, -0.002, 0.01)
    FAST_KEYS = {"cpu"}
    HALF_ONLY = {"gpu", "ssd"}

    def __init__(self):
        self.factor = 1.0
        self.limiting: Reading | None = None   # the component holding the share down
        self._loops: dict[str, dict] = {}
        self._last: float | None = None

    def update(self, now: float, readings: list[Reading], cfg: dict) -> float:
        dt = 0.0 if self._last is None else min(5.0, max(0.0, now - self._last))
        self._last = now
        if not (cfg["enabled"] and cfg["regulate"]):
            self._loops.clear()
            self.factor, self.limiting = 1.0, None
            return self.factor
        lowest, who = 1.0, None
        for r in readings:
            lp = self._loops.setdefault(r.key, {"f": 1.0, "avg": None, "trend": deque()})
            avg_s, trend_s, k_down, k_up, up_max, cooling, heating = self.FAST if r.key in self.FAST_KEYS else self.SLOW
            floor = cfg["min_cpu_factor"]
            if r.key in self.HALF_ONLY:
                floor = max(floor, 0.5)
            if r.temp is None:  # asleep or unreadable: no reason to hold the CPU back
                lp["avg"] = None
                lp["trend"].clear()
                lp["f"] += up_max * dt
            else:
                prev = lp["avg"]
                lp["avg"] = r.temp if prev is None else prev + dt / (avg_s + dt) * (r.temp - prev)
                trend = lp["trend"]
                trend.append((now, lp["avg"]))
                while trend[0][0] < now - trend_s:
                    trend.popleft()
                span = trend[-1][0] - trend[0][0]
                slope = (trend[-1][1] - trend[0][1]) / span if span > 0 else 0.0
                err = lp["avg"] - r.target
                if err > 0 and slope > cooling:                 # too warm and not cooling yet
                    lp["f"] -= k_down * min(err, 10.0) * dt
                elif err < -self.BAND and slope < heating:      # comfortably cool and not heating fast
                    lp["f"] += min(up_max, k_up * (-err - self.BAND)) * dt
            lp["f"] = max(floor, min(1.0, lp["f"]))
            if lp["f"] < lowest:
                lowest, who = lp["f"], r
        for key in set(self._loops) - {r.key for r in readings}:
            del self._loops[key]
        self.factor, self.limiting = lowest, (who if lowest < 0.999 else None)
        return self.factor

    def after_freeze(self, share: float = 0.5) -> None:
        """Tasks come back from an overheating freeze on part of the CPU and get the rest back gradually."""
        for lp in self._loops.values():
            lp["f"] = min(lp["f"], share)
        self.factor = min(self.factor, share)


# ---------------------------------------------------------------- sensors (Windows)
ZONE_PLAUSIBLE_C = (20.0, 125.0)   # a powered PC's own sensor is never colder than a room, nor this hot
ZONE_THROTTLE_MIN_C = 50.0         # firmware does not slow a CPU down for heat while its zone is cooler than this


def zone_reading(kelvin: dict[str, float], passive: dict[str, float],
                 reasons: dict[str, float]) -> tuple[float | None, bool]:
    """(the hottest real zone in °C, whether the firmware throttles for heat) from the per-zone counters.

    Many desktops expose dummy ACPI zones: a fixed, impossible temperature (17 °C was seen) and a passive-cooling
    flag that never clears. Such zones are ignored, and the throttle flag counts only for a zone that is really
    warm, so a dummy can never freeze the tasks for good."""
    lo, hi = ZONE_PLAUSIBLE_C
    real = {name: k - 273.15 for name, k in kelvin.items() if lo <= k - 273.15 <= hi}
    throttling = any(t >= ZONE_THROTTLE_MIN_C and (passive.get(name, 100.0) < 99.5 or reasons.get(name, 0.0) > 0)
                     for name, t in real.items())
    return (round(max(real.values()), 1) if real else None), throttling


class CpuZones:
    """ACPI thermal zones (Kelvin) and the firmware's passive-cooling throttle flag."""

    def __init__(self):
        self._q = winapi.PdhQuery()
        self.ok = self._q.add("t", r"\Thermal Zone Information(*)\Temperature", winapi.PDH_FMT_DOUBLE)
        self._passive = self._q.add("p", r"\Thermal Zone Information(*)\% Passive Limit", winapi.PDH_FMT_DOUBLE)
        self._reasons = self._q.add("r", r"\Thermal Zone Information(*)\Throttle Reasons", winapi.PDH_FMT_DOUBLE)
        if self.ok:
            self._q.collect()

    def read(self) -> tuple[float | None, bool]:
        if not self.ok:
            return None, False
        self._q.collect()
        return zone_reading(self._q.values("t"), self._q.values("p") if self._passive else {},
                            self._q.values("r") if self._reasons else {})


def _number(text: str) -> float | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", text or "")
    return float(m.group(0).replace(",", ".")) if m else None


def parse_lhm(tree: dict) -> float | None:
    """CPU package temperature from LibreHardwareMonitor's data.json (hottest core as a fallback)."""
    found: dict[str, float] = {}

    def walk(node: dict) -> None:
        sid = node.get("SensorId", "")
        if "cpu/" in sid and "/temperature/" in sid:
            v = _number(node.get("Value", ""))
            if v is not None:
                found.setdefault(node.get("Text", ""), v)
        for child in node.get("Children", []):
            walk(child)

    walk(tree)
    for name in ("CPU Package", "Core Max", "Core (Tctl/Tdie)", "Core (Tctl)"):
        if name in found:
            return found[name]
    cores = [v for k, v in found.items() if "Distance" not in k]
    return max(cores) if cores else None


_CORE_CLOCK = re.compile(r"(P-Core|E-Core|CPU Core|Core) #\d+")


def parse_lhm_cpu(tree: dict) -> dict:
    """The CPU's package power ("power", W) and its cores' average clock ("clock_p", "clock_e", GHz: P- and E-cores
    apart on a hybrid CPU, all cores in "clock_p" otherwise) from LibreHardwareMonitor's data.json. What it does
    not show is left out."""
    power: dict[str, float] = {}
    clocks: dict[str, list[float]] = {"clock_p": [], "clock_e": []}

    def walk(node: dict) -> None:
        sid = node.get("SensorId", "")
        if "cpu/" in sid:
            name = node.get("Text", "")
            v = _number(node.get("Value", ""))
            if v is not None and "/power/" in sid:
                power.setdefault(name, v)
            elif v is not None and v > 0 and "/clock/" in sid and _CORE_CLOCK.fullmatch(name):
                clocks["clock_e" if name.startswith("E-") else "clock_p"].append(v)
        for child in node.get("Children", []):
            walk(child)

    walk(tree)
    out = {}
    for name in ("CPU Package", "Package"):
        if name in power:
            out["power"] = round(power[name], 1)
            break
    for k, vals in clocks.items():
        if vals:
            out[k] = round(sum(vals) / len(vals) / 1000, 2)
    return out


def parse_lhm_ranges(tree: dict) -> dict:
    """The lowest and highest package power (W) and core clock (GHz) LibreHardwareMonitor has seen since it
    started - the range this CPU really works in: {"power": [lo, hi], "clock": [lo, hi]}, what it shows."""
    power: dict[str, tuple] = {}
    lo_clock = hi_clock = None

    def walk(node: dict) -> None:
        nonlocal lo_clock, hi_clock
        sid = node.get("SensorId", "")
        if "cpu/" in sid:
            name = node.get("Text", "")
            lo, hi = _number(node.get("Min", "")), _number(node.get("Max", ""))
            if "/power/" in sid and lo is not None and hi is not None:
                power.setdefault(name, (lo, hi))
            elif "/clock/" in sid and _CORE_CLOCK.fullmatch(name):
                if lo is not None and lo > 0:
                    lo_clock = lo if lo_clock is None else min(lo_clock, lo)
                if hi is not None and hi > 0:
                    hi_clock = hi if hi_clock is None else max(hi_clock, hi)
        for child in node.get("Children", []):
            walk(child)

    walk(tree)
    out = {}
    for name in ("CPU Package", "Package"):
        if name in power and power[name][1] > 0:
            out["power"] = [round(power[name][0], 1), round(power[name][1], 1)]
            break
    if lo_clock is not None and hi_clock is not None:
        out["clock"] = [round(lo_clock / 1000, 2), round(hi_clock / 1000, 2)]
    return out


class LhmCpu:
    """Reads the CPU temperature from LibreHardwareMonitor's local web server, and with it the CPU's package power
    and core clocks (`extra`, see parse_lhm_cpu; empty while it cannot be read).

    Installed by tools/install_cpu_sensors.ps1 (Program Files; port and password from its settings), or started by
    the owner from any folder: then its settings lie next to its exe, and without them the default port is tried
    with no password. Whether it runs and whether its port is open is looked up every half a minute, not on every
    reading: a closed port on this PC answers only after ~2 s on Windows, which would stall the manager's loop."""

    CONFIG = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreHardwareMonitor" / "LibreHardwareMonitor.config"
    PORT = 8085            # its default
    FIND_EVERY_S = 30.0

    def __init__(self):
        # нет | работает | не отвечает | веб-сервер выключен | закрыт паролем
        self.status = "нет"
        self._stamp: float | None = None
        self._auth: str | None = None
        self._good_auth: str | None = None
        self._port = self.PORT
        self._config: Path | None = None
        self._running = False
        self._open = True                # someone listens on its port
        self._found_at = -1e9
        self.extra: dict = {}
        self.ranges: dict = {}           # parse_lhm_ranges: the range the CPU works in

    @staticmethod
    def _process() -> tuple[bool, Path | None]:
        """(whether LibreHardwareMonitor runs, its exe when this process may see it)."""
        try:
            import psutil
        except ImportError:
            return False, None
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == "librehardwaremonitor.exe":
                try:
                    return True, Path(p.exe())
                except (psutil.Error, OSError, ValueError):
                    return True, None  # it runs elevated: its folder may be hidden from a normal process
        return False, None

    @staticmethod
    def _listening(port: int) -> bool:
        try:
            import psutil
            return any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port
                       for c in psutil.net_connections(kind="tcp"))
        except Exception:
            return True  # cannot tell: just try it

    def _find(self) -> None:
        now = time.monotonic()
        if now - self._found_at < self.FIND_EVERY_S:
            return
        self._found_at = now
        self._running, exe = self._process()
        near = exe.with_name("LibreHardwareMonitor.config") if exe else None
        config = self.CONFIG if self.CONFIG.exists() else near if near and near.exists() else None
        if config != self._config:
            self._config, self._stamp, self._auth, self._good_auth, self._port = config, None, None, None, self.PORT
        if self._config is not None:
            try:
                self._load()
            except (OSError, ValueError):
                pass
        self._open = self._listening(self._port)

    def _load(self) -> None:
        # LibreHardwareMonitor re-hashes its stored password on every save, so the current file is the truth
        stamp = self._config.stat().st_mtime
        if stamp == self._stamp:
            return
        vals = dict(re.findall(r'<add key="([^"]+)" value="([^"]*)"', self._config.read_text(encoding="utf-8")))
        self._port = int(vals.get("listenerPort") or self.PORT)
        auth = None
        if vals.get("authenticationEnabled") == "true":
            pair = f'{vals.get("authenticationUserName", "")}:{vals.get("authenticationPassword", "")}'
            auth = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
        self._auth, self._stamp = auth, stamp

    def _get(self, auth: str | None) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{self._port}/data.json",
                                     headers={"Authorization": auth} if auth else {})
        with urllib.request.urlopen(req, timeout=1.0) as r:
            return json.load(r)

    def read(self) -> float | None:
        self.extra = {}
        self._find()
        if self._config is None and not self._running:
            self.status = "нет"
            return None
        if not self._open:
            self.status = "веб-сервер выключен" if self._running else "не отвечает"
            return None
        try:
            if self._config is not None:
                self._load()
            try:
                tree = self._get(self._auth)
                self._good_auth = self._auth
            except urllib.error.HTTPError as e:
                if e.code != 401 or not self._good_auth or self._good_auth == self._auth:
                    raise
                tree = self._get(self._good_auth)
        except urllib.error.HTTPError as e:
            self.status = "закрыт паролем" if e.code == 401 and self._auth is None else "не отвечает"
            return None
        except (OSError, ValueError):
            self.status = "не отвечает"
            self._found_at = -1e9  # it may have closed: look again before the next try
            return None
        self.status = "работает"
        self.extra = parse_lhm_cpu(tree)
        self.ranges = parse_lhm_ranges(tree)
        return parse_lhm(tree)


class NvidiaGpu:
    """NVML from the NVIDIA driver; opened only for the time the GPU is being watched."""

    THERMAL_REASONS = 0x20 | 0x40  # SW / HW thermal slowdown

    def __init__(self):
        try:
            self._nv = ctypes.WinDLL("nvml.dll")
        except OSError:
            self._nv = None
        self.present = self._nv is not None and self._open()
        self.max_operating: int | None = None
        self.power_limit: float | None = None   # W, the board power the driver enforces
        self.max_clock: float | None = None     # GHz, the graphics clock's ceiling
        self.extra: dict = {}                   # {"power": W, "clock": GHz} while it is watched
        if self.present:
            v = ctypes.c_uint(0)
            if self._nv.nvmlDeviceGetTemperatureThreshold(self._h, 3, ctypes.byref(v)) == 0:  # GPU_MAX
                self.max_operating = v.value
            if self._nv.nvmlDeviceGetEnforcedPowerLimit(self._h, ctypes.byref(v)) == 0 and v.value:
                self.power_limit = round(v.value / 1000, 1)
            if self._nv.nvmlDeviceGetMaxClockInfo(self._h, 0, ctypes.byref(v)) == 0 and v.value:  # graphics
                self.max_clock = round(v.value / 1000, 2)
            self._close()

    def _open(self) -> bool:
        if self._nv.nvmlInit_v2() != 0:
            return False
        self._h = ctypes.c_void_p()
        if self._nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self._h)) != 0:
            self._nv.nvmlShutdown()
            return False
        self._reasons_fn = (getattr(self._nv, "nvmlDeviceGetCurrentClocksEventReasons", None)
                            or self._nv.nvmlDeviceGetCurrentClocksThrottleReasons)
        self.open = True
        return True

    def _close(self) -> None:
        if getattr(self, "open", False):
            self._nv.nvmlShutdown()
            self.open = False

    def read(self, watch: bool) -> tuple[float | None, bool]:
        """Temperature and thermal throttling while `watch` (and its power and clock in `extra`); otherwise the
        library is released."""
        self.extra = {}
        if not self.present:
            return None, False
        if not watch:
            self._close()
            return None, False
        if not getattr(self, "open", False) and not self._open():
            return None, False
        t = ctypes.c_uint(0)
        if self._nv.nvmlDeviceGetTemperature(self._h, 0, ctypes.byref(t)) != 0:
            return None, False
        r = ctypes.c_ulonglong(0)
        throttling = self._reasons_fn(self._h, ctypes.byref(r)) == 0 and bool(r.value & self.THERMAL_REASONS)
        v = ctypes.c_uint(0)
        if self._nv.nvmlDeviceGetPowerUsage(self._h, ctypes.byref(v)) == 0:
            self.extra["power"] = round(v.value / 1000, 1)
        if self._nv.nvmlDeviceGetClockInfo(self._h, 0, ctypes.byref(v)) == 0 and v.value:
            self.extra["clock"] = round(v.value / 1000, 2)
        return float(t.value), throttling

    def close(self) -> None:
        if self._nv is not None:
            self._close()


class Drives:
    """Temperatures of the system's drives and the limits the drives report about themselves."""

    IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
    StorageDeviceTemperatureProperty = 52

    def __init__(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wt.HANDLE
        k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
        k.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                      ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
        self._k = k
        self.drives = [n for n in range(8) if self._query(n) is not None]

    def _query(self, n: int):
        h = self._k.CreateFileW(r"\\.\PhysicalDrive%d" % n, 0, 3, None, 3, 0, None)  # no access rights needed
        if h in (None, wt.HANDLE(-1).value):
            return None
        try:
            q = struct.pack("<II4s", self.StorageDeviceTemperatureProperty, 0, b"\0" * 4)
            buf = ctypes.create_string_buffer(512)
            got = wt.DWORD(0)
            if not self._k.DeviceIoControl(h, self.IOCTL_STORAGE_QUERY_PROPERTY, q, len(q), buf, 512,
                                           ctypes.byref(got), None):
                return None
            _ver, _size, critical, warning, count = struct.unpack_from("<IIhhH", buf.raw, 0)
            temps = [struct.unpack_from("<Hh", buf.raw, 24 + 16 * i)[1] for i in range(min(count, 8))]
            temps = [t for t in temps if -20 < t < 150]
            return (max(temps) if temps else None), warning, critical
        finally:
            winapi.CloseHandle(h)

    def read(self) -> tuple[float | None, int | None]:
        """(hottest drive temperature, the lowest warning limit reported by a drive)."""
        best, warn = None, None
        for n in self.drives:
            r = self._query(n)
            if r and r[0] is not None:
                best = r[0] if best is None else max(best, r[0])
                if r[1] and r[1] > 0:
                    warn = r[1] if warn is None else min(warn, r[1])
        return (float(best) if best is not None else None), warn
