"""ctypes bindings for the Win32 / NT APIs the manager needs (Windows 10+).

Everything here is a thin wrapper: job objects (CPU caps, affinity, kill),
process suspend/resume and priorities, system memory/CPU counters, CPU-set
topology (P/E cores), PDH performance counters (per-process GPU memory and
engine load), DXGI adapter enumeration and the shell "fullscreen app" state.
"""
from __future__ import annotations

import ctypes
import os
import ctypes.wintypes as wt
import struct
import sys
import uuid

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("rmq.winapi requires Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
shell32 = ctypes.WinDLL("shell32")
pdh = ctypes.WinDLL("pdh")

HANDLE = wt.HANDLE
ULONG_PTR = ctypes.c_size_t


def _check_bool(result, func, args):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _fn(dll, name, restype, *argtypes, check=False):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    if check:
        f.errcheck = _check_bool
    return f


# ---------------------------------------------------------------- constants
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
PROCESS_CONTROL = (PROCESS_TERMINATE | PROCESS_SET_QUOTA | PROCESS_SET_INFORMATION
                   | PROCESS_QUERY_INFORMATION | PROCESS_SUSPEND_RESUME)

JOB_OBJECT_ALL_ACCESS = 0x1F001F
JOB_OBJECT_QUERY = 0x0004

IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200

JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4

JobObjectBasicAccountingInformation = 1
JobObjectBasicProcessIdList = 3
JobObjectBasicAndIoAccountingInformation = 8
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15

ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_MORE_DATA = 234


# ---------------------------------------------------------------- structures
class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

    def value(self) -> int:
        return (self.hi << 32) | self.lo


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wt.DWORD),
        ("TotalProcesses", wt.DWORD),
        ("ActiveProcesses", wt.DWORD),
        ("TotalTerminatedProcesses", wt.DWORD),
    ]


class JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicInfo", JOBOBJECT_BASIC_ACCOUNTING_INFORMATION), ("IoInfo", IO_COUNTERS)]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [("ControlFlags", wt.DWORD), ("CpuRate", wt.DWORD)]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wt.DWORD),
        ("ProcessCount", wt.DWORD),
        ("ThreadCount", wt.DWORD),
    ]


class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]


# ---------------------------------------------------------------- functions
CloseHandle = _fn(kernel32, "CloseHandle", wt.BOOL, HANDLE)
OpenProcess = _fn(kernel32, "OpenProcess", HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
CreateJobObjectW = _fn(kernel32, "CreateJobObjectW", HANDLE, ctypes.c_void_p, wt.LPCWSTR)
OpenJobObjectW = _fn(kernel32, "OpenJobObjectW", HANDLE, wt.DWORD, wt.BOOL, wt.LPCWSTR)
AssignProcessToJobObject = _fn(kernel32, "AssignProcessToJobObject", wt.BOOL, HANDLE, HANDLE, check=True)
SetInformationJobObject = _fn(kernel32, "SetInformationJobObject", wt.BOOL,
                              HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, check=True)
QueryInformationJobObject = _fn(kernel32, "QueryInformationJobObject", wt.BOOL,
                                HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                ctypes.POINTER(wt.DWORD))
TerminateJobObject = _fn(kernel32, "TerminateJobObject", wt.BOOL, HANDLE, wt.UINT, check=True)
GetSystemTimes = _fn(kernel32, "GetSystemTimes", wt.BOOL, ctypes.POINTER(FILETIME),
                     ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), check=True)
K32GetPerformanceInfo = _fn(kernel32, "K32GetPerformanceInfo", wt.BOOL, ctypes.c_void_p, wt.DWORD, check=True)
K32EmptyWorkingSet = _fn(kernel32, "K32EmptyWorkingSet", wt.BOOL, HANDLE)
SetProcessInformation = _fn(kernel32, "SetProcessInformation", wt.BOOL,
                            HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)
GetSystemCpuSetInformation = _fn(kernel32, "GetSystemCpuSetInformation", wt.BOOL,
                                 ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG), HANDLE, wt.ULONG)
# Same calls Resource Monitor / Process Explorer / psutil use for "suspend process" and
# Process Lasso uses for "I/O priority". They are applied only to the manager's own task processes.
NtSuspendProcess = _fn(ntdll, "NtSuspendProcess", ctypes.c_long, HANDLE)
NtResumeProcess = _fn(ntdll, "NtResumeProcess", ctypes.c_long, HANDLE)
NtSetInformationProcess = _fn(ntdll, "NtSetInformationProcess", ctypes.c_long,
                              HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG)
user32 = ctypes.WinDLL("user32")
GetForegroundWindow = _fn(user32, "GetForegroundWindow", wt.HWND)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
SHQueryUserNotificationState = _fn(shell32, "SHQueryUserNotificationState", ctypes.c_long,
                                   ctypes.POINTER(ctypes.c_int))


# ---------------------------------------------------------------- system info
def system_times() -> tuple[int, int, int]:
    """(idle, kernel, user) in 100 ns units; kernel includes idle."""
    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    return idle.value(), kern.value(), user.value()


def performance_info() -> dict:
    """Physical memory and commit charge, in bytes."""
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    K32GetPerformanceInfo(ctypes.byref(pi), pi.cb)
    page = pi.PageSize
    return {
        "ram_total": pi.PhysicalTotal * page,
        "ram_available": pi.PhysicalAvailable * page,
        "commit_total": pi.CommitTotal * page,
        "commit_limit": pi.CommitLimit * page,
        "system_cache": pi.SystemCache * page,
    }


def cpu_sets() -> list[dict]:
    """Logical processors with their core index and efficiency class.

    On hybrid CPUs (Intel 12th gen+) P-cores report a higher EfficiencyClass
    than E-cores; on uniform CPUs every entry has the same class.
    """
    needed = wt.ULONG(0)
    GetSystemCpuSetInformation(None, 0, ctypes.byref(needed), None, 0)
    if not needed.value:
        return []
    buf = ctypes.create_string_buffer(needed.value)
    if not GetSystemCpuSetInformation(buf, needed, ctypes.byref(needed), None, 0):
        return []
    raw, out, off = buf.raw, [], 0
    while off + 8 <= needed.value:
        size, kind = struct.unpack_from("<II", raw, off)
        if size == 0:
            break
        if kind == 0 and size >= 20:  # CpuSetInformation
            _id, group, logical, core, _llc, _numa, eff, flags = struct.unpack_from("<IHBBBBBB", raw, off + 8)
            out.append({"group": group, "logical": logical, "core": core,
                        "efficiency": eff, "parked": bool(flags & 1)})
        off += size
    return out


QUNS_RUNNING_D3D_FULL_SCREEN = 3


def user_notification_state() -> int:
    """SHQueryUserNotificationState: 3 means a fullscreen Direct3D app is running."""
    state = ctypes.c_int(0)
    if SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return 0
    return state.value


# ---------------------------------------------------------------- power
class _REASON_DETAILED(ctypes.Structure):
    _fields_ = [("LocalizedReasonModule", ctypes.c_void_p), ("LocalizedReasonId", wt.ULONG),
                ("ReasonStringCount", wt.ULONG), ("ReasonStrings", ctypes.c_void_p)]


class _REASON_UNION(ctypes.Union):
    _fields_ = [("Detailed", _REASON_DETAILED), ("SimpleReasonString", ctypes.c_wchar_p)]


class REASON_CONTEXT(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("Flags", wt.DWORD), ("Reason", _REASON_UNION)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


PowerCreateRequest = _fn(kernel32, "PowerCreateRequest", HANDLE, ctypes.POINTER(REASON_CONTEXT))
PowerSetRequest = _fn(kernel32, "PowerSetRequest", wt.BOOL, HANDLE, ctypes.c_int)
PowerClearRequest = _fn(kernel32, "PowerClearRequest", wt.BOOL, HANDLE, ctypes.c_int)
GetSystemPowerStatus = _fn(kernel32, "GetSystemPowerStatus", wt.BOOL, ctypes.POINTER(SYSTEM_POWER_STATUS))
QueryUnbiasedInterruptTime = _fn(kernel32, "QueryUnbiasedInterruptTime", wt.BOOL, ctypes.POINTER(ctypes.c_ulonglong))
PowerRequestSystemRequired = 1


class KeepAwake:
    """While held, Windows does not go to sleep on its idle timer; the display still turns off.

    The documented power-request API (what video players and backup tools use): it changes no
    settings, shows up in `powercfg /requests` with our reason, and disappears with the process.
    Closing the lid, the power button and Start -> Sleep still put the PC to sleep, as they should.
    """

    def __init__(self, reason: str):
        self._ctx = REASON_CONTEXT(0, 0x1)  # POWER_REQUEST_CONTEXT_VERSION, POWER_REQUEST_CONTEXT_SIMPLE_STRING
        self._ctx.Reason.SimpleReasonString = reason
        self._h = PowerCreateRequest(ctypes.byref(self._ctx))
        self.active = False

    def set(self, on: bool) -> bool:
        if not self._h or self._h == HANDLE(-1).value or on == self.active:
            return self.active
        ok = (PowerSetRequest if on else PowerClearRequest)(self._h, PowerRequestSystemRequired)
        if ok:
            self.active = on
        return self.active

    def close(self) -> None:
        self.set(False)
        if self._h:
            CloseHandle(self._h)
            self._h = None


def on_battery() -> tuple[bool, int | None]:
    """(running on battery, battery percent or None)."""
    st = SYSTEM_POWER_STATUS()
    if not GetSystemPowerStatus(ctypes.byref(st)):
        return False, None
    pct = st.BatteryLifePercent if st.BatteryLifePercent != 255 else None
    return st.ACLineStatus == 0, pct


# the "Power mode" slider of Windows (an overlay over the power plan); none set means the balanced middle
POWER_MODES = {"961cc777-2547-4f9d-8174-7d86181b8a7a": "экономия энергии",
               "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "экономия энергии",
               "00000000-0000-0000-0000-000000000000": "сбалансированный",
               "381b4222-f694-41f0-9685-ff5bb260df2e": "сбалансированный",
               "ded574b5-45a0-4f42-8737-46345c09c238": "максимальная производительность"}


def power_mode(battery: bool) -> dict:
    """{"mode": the Windows power mode for the current power source, "plan": the power plan's name}; what cannot
    be read is left out. Laptop makers' own modes (a key like Fn+Q) are not visible here."""
    out: dict = {}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes") as k:
            try:
                overlay = str(winreg.QueryValueEx(k, "ActiveOverlayDcPowerScheme" if battery
                                                  else "ActiveOverlayAcPowerScheme")[0] or "").lower()
            except OSError:
                overlay = ""
        out["mode"] = POWER_MODES.get(overlay or "00000000-0000-0000-0000-000000000000", "свой")
    except OSError:
        pass
    try:
        pp = ctypes.WinDLL("powrprof.dll")
        scheme = ctypes.c_void_p()
        if pp.PowerGetActiveScheme(None, ctypes.byref(scheme)) == 0 and scheme.value:
            try:
                size = wt.DWORD(0)
                pp.PowerReadFriendlyName(None, scheme, None, None, None, ctypes.byref(size))
                buf = ctypes.create_string_buffer(size.value or 2)
                if pp.PowerReadFriendlyName(None, scheme, None, None, buf, ctypes.byref(size)) == 0:
                    out["plan"] = buf.raw.decode("utf-16-le", errors="replace").rstrip("\0")
            finally:
                ctypes.windll.kernel32.LocalFree(scheme)
    except (OSError, AttributeError):
        pass
    return out


def unbiased_seconds() -> float:
    """Time since boot that does not advance while the PC sleeps or hibernates."""
    v = ctypes.c_ulonglong(0)
    QueryUnbiasedInterruptTime(ctypes.byref(v))
    return v.value / 1e7


# ---------------------------------------------------------------- processes
def open_process(pid: int, access: int = PROCESS_CONTROL):
    h = OpenProcess(access, False, pid)
    return h or None


def suspend_process(h) -> bool:
    return NtSuspendProcess(h) >= 0


def resume_process(h) -> bool:
    return NtResumeProcess(h) >= 0


def empty_working_set(h) -> bool:
    """Push the process' pages out of RAM (to standby/modified lists -> pagefile)."""
    return bool(K32EmptyWorkingSet(h))


def set_low_io_priority(h) -> None:
    """Disk reads/writes of this process yield to everyone else's (very low I/O priority)."""
    io = wt.ULONG(0)  # IoPriorityVeryLow
    NtSetInformationProcess(h, 33, ctypes.byref(io), 4)  # ProcessIoPriority


def foreground_pid() -> int:
    """Process that owns the active window (0 if none)."""
    hwnd = GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wt.DWORD(0)
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def set_low_memory_priority(h) -> None:
    """Under memory pressure Windows reclaims this process' pages before the user's apps."""
    mem = wt.ULONG(1)  # MEMORY_PRIORITY_VERY_LOW
    SetProcessInformation(h, 0, ctypes.byref(mem), 4)  # ProcessMemoryPriority


def set_eco_qos(h, enabled: bool) -> None:
    """EcoQoS / power throttling: the OS prefers efficient cores and lower clocks."""
    st = PROCESS_POWER_THROTTLING_STATE(1, 0x1, 0x1 if enabled else 0x0)
    SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))  # ProcessPowerThrottling


# ---------------------------------------------------------------- low integrity (tasks from the network)
# A task from another PC runs at the "low" integrity level, the one browsers use for web content: it may
# read the owner's files but not change or delete them. It writes only under AppData\LocalLow, which
# Windows itself labels for low-integrity processes. Lowering a process' own token needs no admin rights.
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                ("hStdError", HANDLE)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", HANDLE)
GetExitCodeProcess = _fn(kernel32, "GetExitCodeProcess", wt.BOOL, HANDLE, ctypes.POINTER(wt.DWORD))
TerminateProcess = _fn(kernel32, "TerminateProcess", wt.BOOL, HANDLE, wt.UINT)
LocalFree = _fn(kernel32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
OpenProcessToken = _fn(advapi32, "OpenProcessToken", wt.BOOL, HANDLE, wt.DWORD, ctypes.POINTER(HANDLE))
DuplicateTokenEx = _fn(advapi32, "DuplicateTokenEx", wt.BOOL, HANDLE, wt.DWORD, ctypes.c_void_p, ctypes.c_int,
                       ctypes.c_int, ctypes.POINTER(HANDLE))
ConvertStringSidToSidW = _fn(advapi32, "ConvertStringSidToSidW", wt.BOOL, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
GetLengthSid = _fn(advapi32, "GetLengthSid", wt.DWORD, ctypes.c_void_p)
SetTokenInformation = _fn(advapi32, "SetTokenInformation", wt.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD)
CreateProcessAsUserW = _fn(advapi32, "CreateProcessAsUserW", wt.BOOL, HANDLE, wt.LPCWSTR, wt.LPWSTR,
                           ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
                           ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION))
STILL_ACTIVE = 259


def local_low() -> str:
    """%USERPROFILE%/AppData/LocalLow: the folder Windows lets low-integrity processes write to."""
    return os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"), "AppData", "LocalLow")


def _low_integrity_token():
    tok = HANDLE()
    # TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT
    if not OpenProcessToken(GetCurrentProcess(), 0x0002 | 0x0008 | 0x0001 | 0x0080, ctypes.byref(tok)):
        raise ctypes.WinError(ctypes.get_last_error())
    dup = HANDLE()
    try:
        if not DuplicateTokenEx(tok, 0x02000000, None, 2, 1, ctypes.byref(dup)):  # MAXIMUM_ALLOWED, primary
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        CloseHandle(tok)
    sid = ctypes.c_void_p()
    if not ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)):  # the low mandatory level
        CloseHandle(dup)
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        label = _TOKEN_MANDATORY_LABEL(_SID_AND_ATTRIBUTES(sid, 0x20))  # SE_GROUP_INTEGRITY
        if not SetTokenInformation(dup, 25, ctypes.byref(label), ctypes.sizeof(label) + GetLengthSid(sid)):
            CloseHandle(dup)
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        LocalFree(sid)
    return dup


class RawProcess:
    """A process started through CreateProcessAsUser: the bits of subprocess.Popen the manager uses."""

    def __init__(self, pid: int, handle):
        self.pid = pid
        self._h = handle

    def poll(self) -> int | None:
        if not self._h:
            return None
        code = wt.DWORD(0)
        if not GetExitCodeProcess(self._h, ctypes.byref(code)) or code.value == STILL_ACTIVE:
            return None
        return code.value

    def kill(self) -> None:
        if self._h:
            TerminateProcess(self._h, 1)

    def close(self) -> None:
        if self._h:
            CloseHandle(self._h)
            self._h = None


def create_low_integrity_process(argv: list[str], *, cwd: str, env: dict[str, str], flags: int) -> RawProcess:
    """Start a suspended process at low integrity, without console handles (it writes its own log)."""
    import subprocess
    token = _low_integrity_token()
    try:
        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        pi = _PROCESS_INFORMATION()
        block = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
        envbuf = ctypes.create_unicode_buffer(block, len(block))
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        ok = CreateProcessAsUserW(token, None, cmd, None, None, False, flags | CREATE_UNICODE_ENVIRONMENT,
                                  envbuf, cwd, ctypes.byref(si), ctypes.byref(pi))
        if not ok and flags & CREATE_BREAKAWAY_FROM_JOB:  # the manager itself sits in a job that forbids it
            ok = CreateProcessAsUserW(token, None, cmd, None, None, False,
                                      (flags & ~CREATE_BREAKAWAY_FROM_JOB) | CREATE_UNICODE_ENVIRONMENT,
                                      envbuf, cwd, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        CloseHandle(token)
    CloseHandle(pi.hThread)  # the process is resumed with NtResumeProcess, like the others
    return RawProcess(pi.dwProcessId, pi.hProcess)


# ---------------------------------------------------------------- job objects
def create_job(name: str | None):
    h = CreateJobObjectW(None, name)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def open_job(name: str, access: int = JOB_OBJECT_ALL_ACCESS):
    h = OpenJobObjectW(access, False, name)
    return h or None


def job_set_limits(h, *, priority_class: int, affinity: int | None, job_memory: int | None) -> None:
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    flags = JOB_OBJECT_LIMIT_PRIORITY_CLASS
    info.BasicLimitInformation.PriorityClass = priority_class
    if affinity:
        flags |= JOB_OBJECT_LIMIT_AFFINITY
        info.BasicLimitInformation.Affinity = affinity
    if job_memory:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = job_memory
    info.BasicLimitInformation.LimitFlags = flags
    SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))


def job_set_cpu_rate(h, rate: int | None) -> None:
    """Hard CPU cap in 1/100 of a percent of the whole machine (1..10000); None removes it."""
    info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
    if rate is None:
        info.ControlFlags = 0
    else:
        info.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        info.CpuRate = max(1, min(10000, int(rate)))
    SetInformationJobObject(h, JobObjectCpuRateControlInformation, ctypes.byref(info), ctypes.sizeof(info))


def job_accounting(h) -> tuple[float, int]:
    """(total CPU seconds consumed by the job, number of live processes)."""
    info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    if not QueryInformationJobObject(h, JobObjectBasicAccountingInformation, ctypes.byref(info),
                                     ctypes.sizeof(info), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return (info.TotalUserTime + info.TotalKernelTime) / 1e7, info.ActiveProcesses


def job_accounting_io(h) -> tuple[float, int, int, int]:
    """(CPU seconds, live processes, bytes read, bytes written) of the job since it was created."""
    info = JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION()
    if not QueryInformationJobObject(h, JobObjectBasicAndIoAccountingInformation, ctypes.byref(info),
                                     ctypes.sizeof(info), None):
        raise ctypes.WinError(ctypes.get_last_error())
    b, io = info.BasicInfo, info.IoInfo
    return (b.TotalUserTime + b.TotalKernelTime) / 1e7, b.ActiveProcesses, io.ReadTransferCount, io.WriteTransferCount


def job_pids(h) -> list[int]:
    n = 64
    while True:
        size = 8 + 8 * n
        buf = ctypes.create_string_buffer(size)
        ok = QueryInformationJobObject(h, JobObjectBasicProcessIdList, buf, size, None)
        if not ok and ctypes.get_last_error() != ERROR_MORE_DATA:
            raise ctypes.WinError(ctypes.get_last_error())
        assigned, in_list = struct.unpack_from("<II", buf.raw, 0)
        if in_list < assigned:
            n = assigned + 16
            continue
        return list(struct.unpack_from(f"<{in_list}Q", buf.raw, 8))


def job_peak_memory(h) -> int:
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not QueryInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                     ctypes.sizeof(info), None):
        return 0
    return info.PeakJobMemoryUsed


def terminate_job(h, code: int = 1) -> None:
    TerminateJobObject(h, code)


GetDriveTypeW = _fn(kernel32, "GetDriveTypeW", wt.UINT, wt.LPCWSTR)
DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3


def local_drive(root: str) -> bool:
    """A local disk (not a network share or an optical drive): asking it for free space never hangs."""
    return GetDriveTypeW(root) in (DRIVE_REMOVABLE, DRIVE_FIXED)


# ---------------------------------------------------------------- PDH counters
PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_LARGE = 0x00000400
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2


class _PdhValue(ctypes.Union):
    _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                ("largeValue", ctypes.c_longlong), ("ptr", ctypes.c_void_p)]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("CStatus", wt.DWORD), ("u", _PdhValue)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]


PdhOpenQueryW = _fn(pdh, "PdhOpenQueryW", wt.DWORD, wt.LPCWSTR, ULONG_PTR, ctypes.POINTER(HANDLE))
PdhAddEnglishCounterW = _fn(pdh, "PdhAddEnglishCounterW", wt.DWORD, HANDLE, wt.LPCWSTR, ULONG_PTR,
                            ctypes.POINTER(HANDLE))
PdhCollectQueryData = _fn(pdh, "PdhCollectQueryData", wt.DWORD, HANDLE)
PdhGetFormattedCounterArrayW = _fn(pdh, "PdhGetFormattedCounterArrayW", wt.DWORD, HANDLE, wt.DWORD,
                                   ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.c_void_p)
PdhCloseQuery = _fn(pdh, "PdhCloseQuery", wt.DWORD, HANDLE)


class PdhQuery:
    """A PDH query holding wildcard counters, e.g. r'\\GPU Engine(*)\\Utilization Percentage'."""

    def __init__(self):
        self._h = HANDLE()
        status = PdhOpenQueryW(None, 0, ctypes.byref(self._h))
        if status != 0:
            raise OSError(f"PdhOpenQuery failed: 0x{status:08X}")
        self._counters: dict[str, tuple[HANDLE, int]] = {}

    def add(self, key: str, path: str, fmt: int) -> bool:
        hc = HANDLE()
        if PdhAddEnglishCounterW(self._h, path, 0, ctypes.byref(hc)) != 0:
            return False
        self._counters[key] = (hc, fmt)
        return True

    def collect(self) -> None:
        PdhCollectQueryData(self._h)

    def values(self, key: str) -> dict[str, float]:
        if key not in self._counters:
            return {}
        hc, fmt = self._counters[key]
        size, count = wt.DWORD(0), wt.DWORD(0)
        if PdhGetFormattedCounterArrayW(hc, fmt, ctypes.byref(size), ctypes.byref(count), None) != PDH_MORE_DATA:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if PdhGetFormattedCounterArrayW(hc, fmt, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))
        out: dict[str, float] = {}
        large = bool(fmt & PDH_FMT_LARGE)
        for i in range(count.value):
            it = items[i]
            if it.FmtValue.CStatus in (0, 1):
                out[it.szName] = float(it.FmtValue.largeValue if large else it.FmtValue.doubleValue)
        return out

    def close(self) -> None:
        if self._h:
            PdhCloseQuery(self._h)
            self._h = HANDLE()


# ---------------------------------------------------------------- DXGI adapters
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wt.WCHAR * 128),
        ("VendorId", wt.UINT), ("DeviceId", wt.UINT), ("SubSysId", wt.UINT), ("Revision", wt.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wt.UINT),
    ]


def _vtbl_fn(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def dxgi_adapters() -> list[dict]:
    """Hardware graphics adapters: name, vendor id, dedicated VRAM bytes and LUID (for PDH)."""
    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError:
        return []
    create = dxgi.CreateDXGIFactory1
    create.restype = ctypes.c_long
    create.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    iid = _GUID.from_buffer_copy(uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
    factory = ctypes.c_void_p()
    if create(ctypes.byref(iid), ctypes.byref(factory)) < 0 or not factory:
        return []
    out = []
    try:
        enum_adapters1 = _vtbl_fn(factory, 12, ctypes.c_long, wt.UINT, ctypes.POINTER(ctypes.c_void_p))
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters1(factory, i, ctypes.byref(adapter)) < 0:  # DXGI_ERROR_NOT_FOUND
                break
            try:
                desc = _DXGI_ADAPTER_DESC1()
                get_desc1 = _vtbl_fn(adapter, 10, ctypes.c_long, ctypes.POINTER(_DXGI_ADAPTER_DESC1))
                if get_desc1(adapter, ctypes.byref(desc)) >= 0 and not (desc.Flags & 0x2):  # skip software
                    luid = f"luid_0x{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08x}_0x{desc.AdapterLuid.LowPart:08x}"
                    out.append({"name": desc.Description, "vendor": desc.VendorId,
                                "vram": int(desc.DedicatedVideoMemory), "luid": luid})
            finally:
                _vtbl_fn(adapter, 2, wt.ULONG)(adapter)  # Release
            i += 1
    finally:
        _vtbl_fn(factory, 2, wt.ULONG)(factory)
    return out
