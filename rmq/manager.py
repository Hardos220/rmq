"""The manager: samples the machine, runs the planner every tick and carries out its decisions.

It only ever controls processes it started itself (each task lives in its own job
object); everything else on the PC is observed read-only and counted as "the user".
"""
from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
import secrets
import sqlite3
import shutil
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import __version__, chronicle, planner, progress, swing, texts, thermal, winapi
from .db import DB, FINAL_STATES
from .games import GameDetector
from .jobs import GB, TaskRuntime, Usage
from .metrics import Metrics
from .monitor import SystemSampler
from .net import NetService, PeerError
from .netbase import NetError, NetStore, changed_files, collect_files, make_zip, matches_any, requirements_hash, \
    safe_extract, safe_name
from .settings import Settings, SettingsError
from .stats import StatsBook, combine_reports
from .updater import Updater
from .util import PRIORITY_NAMES, parse_duration, parse_priority, parse_size_gb

log = logging.getLogger("rmq")

WRAPPER = Path(__file__).with_name("wrapper.py")
THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
               "VECLIB_MAXIMUM_THREADS", "NUMBA_NUM_THREADS", "RAYON_NUM_THREADS", "POLARS_MAX_THREADS",
               "JULIA_NUM_THREADS")
MODES = ("auto", "game", "paused")
HOSTED_ENV_KEEP = ("SystemRoot", "SystemDrive", "windir", "PATH", "PATHEXT", "COMSPEC", "OS", "NUMBER_OF_PROCESSORS",
                   "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
                   "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME", "COMPUTERNAME", "ProgramData", "ProgramFiles",
                   "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)",
                   "CommonProgramW6432", "PUBLIC", "ALLUSERSPROFILE", "CUDA_PATH", "DriverData")
PROTECTED_ENV = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PYTHONPATH", "PYTHONHOME",
                 "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONNOUSERSITE", "VIRTUAL_ENV"}


def cpu_model() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
    except OSError:
        return platform.processor() or "?"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Identity:
    admin: bool
    agent: dict  # the caller's agent row; the dashboard acts as the built-in "user" agent


def python_for_tasks() -> str:
    """The real interpreter (not a venv redirector, not pythonw) to run the task wrapper."""
    p = Path(getattr(sys, "_base_executable", None) or sys.executable)
    if p.name.lower() == "pythonw.exe" and p.with_name("python.exe").exists():
        p = p.with_name("python.exe")
    return str(p)


def remote_end_time(st: dict, submitted_at: float | None, now: float) -> float:
    """When a task sent to another PC really ended there: this PC may have been off and fetched the results hours
    later. Now, if that PC does not say (an older version) or its answer cannot be right."""
    end = st.get("finished_at")
    if isinstance(end, (int, float)) and (submitted_at or 0) <= end <= now + 300:  # clocks may differ a little
        return min(float(end), now)
    return now


def command_key(text: str) -> str:
    """A command with its numbers taken out: the runs of one script over parts or seeds count as the same."""
    return re.sub(r"\d+(\.\d+)?", "#", " ".join((text or "").split()))


def subprocess_list2cmdline(argv: list[str]) -> str:
    import subprocess
    return subprocess.list2cmdline(argv)


def gb_text(v: float) -> str:
    return f"{v:.1f} ГБ" if v >= 1 else f"{v * 1024:.0f} МБ"


def progress_pace(hist: deque, run_s: float, res: dict) -> dict:
    """A second estimate of the time left, from how fast the progress grew over the last ten minutes of work; and
    whether it disagrees with the one from the share done (a progress that is counted wrong, or a slower phase)."""
    pct = res["pct"]
    if not hist or hist[-1][1] != pct:
        hist.append((run_s, pct))
    out: dict = {}
    if pct >= 100:
        return out
    if run_s - hist[-1][0] >= 600:  # not a step forward in ten minutes of work
        out["stalled_s"] = round(run_s - hist[-1][0])
        return out
    base = next((p for p in reversed(hist) if p[0] <= run_s - 600), hist[0])
    span = run_s - base[0]
    if len(hist) < 2 or span < 120 or pct <= base[1]:
        return out
    pace_eta = (100.0 - pct) * span / (pct - base[1])
    out["eta_pace_s"] = round(pace_eta)
    eta = res.get("eta_s")
    if eta and (pace_eta > eta * 1.6 or pace_eta < eta / 1.6) and abs(pace_eta - eta) > 300:
        out["eta_conflict"] = True
    return out


def after_failed(after_id: int, prev: dict | None) -> str:
    if prev is None:
        return f"задачи #{after_id}, после которой она должна была идти, больше нет"
    return (f"задача #{after_id}, после которой она должна была идти, завершилась без успеха "
            f"({texts.STATE_NAMES.get(prev['state'], prev['state'])})")


def command_text(row: dict) -> str:
    if row.get("command"):
        return row["command"]
    try:
        import subprocess
        return subprocess.list2cmdline(json.loads(row["argv"] or "[]"))
    except (ValueError, TypeError):
        return ""


SCRIPT_EXT = (".py", ".r", ".jl", ".js", ".sh", ".ps1", ".bat", ".cmd", ".m", ".ipynb", ".exe")


def default_name(argv: list[str]) -> str:
    """A readable task name: the script (train.py), the module after -m, or the program itself."""
    def base(a: str) -> str:
        return os.path.basename(a.strip('"').rstrip("\\/")) or a
    for i, a in enumerate(argv):
        if i and a.lower().strip('"').endswith(SCRIPT_EXT):
            return base(a)
        if a == "-m" and i + 1 < len(argv):
            return argv[i + 1]
    return base(argv[0]) if argv else "task"


_NAME_BAD = re.compile(r"[^\w@.\- +()#]", re.UNICODE)


def clean_agent_name(name: str) -> str:
    return _NAME_BAD.sub("_", name.strip())[:64].strip()



def _request_text(row: dict) -> str:
    """A task's request in words: "CPU 4 · ОЗУ 8 ГБ · VRAM 6 ГБ · ~2 ч"."""
    parts = [f"CPU {row['cpu']:g}", f"ОЗУ {row['ram_gb']:g} ГБ"]
    if row.get("vram_gb"):
        parts.append(f"VRAM {row['vram_gb']:g} ГБ")
    if row.get("time_s"):
        parts.append(f"~{texts.fmt_minutes(row['time_s'])}")
    return " · ".join(parts)

class Manager:
    def __init__(self, home: Path):
        self.home = home
        self.tasks_dir = home / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(home / "settings.json")
        self.db = DB(home / "rmq.db")
        self.instance = self.db.kv_get("instance")
        if not self.instance:
            self.instance = secrets.token_hex(4)
            self.db.kv_set("instance", self.instance)
        self.admin_token = self._admin_token()
        self.user_agent_id = self._ensure_user_agent()
        self.mode = self.db.kv_get("mode") or "auto"
        if self.mode not in MODES:
            self.mode = "auto"

        self.sampler = SystemSampler(self.settings.section("gpu")["adapter"])
        self.games = GameDetector()
        self.metrics = Metrics()
        self.n_cpu = os.cpu_count() or 1
        self.all_mask = (1 << self.n_cpu) - 1 if self.n_cpu <= 64 else 0
        self.ecore_mask = 0
        sets = winapi.cpu_sets()
        classes = {c["efficiency"] for c in sets}
        if len(classes) > 1 and self.all_mask:
            low = min(classes)
            for c in sets:
                if c["efficiency"] == low and c["group"] == 0:
                    self.ecore_mask |= 1 << c["logical"]
        self.n_ecores = bin(self.ecore_mask).count("1")
        half = self.n_cpu // 2
        self.game_mask = self.ecore_mask or (self.all_mask & ~((1 << half) - 1) if self.all_mask else 0)
        self.n_game_cores = bin(self.game_mask).count("1")
        try:
            self.game_profiles: dict[str, dict] = json.loads(self.db.kv_get("game_profiles") or "{}")
        except ValueError:
            self.game_profiles = {}
        self._profiles_dirty = False

        self.lock = threading.RLock()
        self.runtimes: dict[int, TaskRuntime] = {}
        self.draining: dict[int, TaskRuntime] = {}
        self.live: dict[int, dict] = {}
        self.agent_usage: dict[int, float] = {a["id"]: a["usage"] for a in self.db.agents()}
        self._win: dict[str, planner.WindowMax] = {}
        self.gpu_busy = planner.Debounce(3, 60)
        self.last_shed_at = -1e18
        self.mask = self.all_mask
        self.state: dict = {}
        self.plan: planner.Plan | None = None
        self._last_tick: float | None = None
        self._last_flush = 0.0
        self._last_usage_flush = 0.0
        self._last_cleanup = 0.0
        self._stop = threading.Event()
        self.wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.keep_awake = winapi.KeepAwake("Менеджер ресурсов: идут расчёты агентов (экран может гаснуть)")
        self.cpu_zones = thermal.CpuZones()
        self.nv = thermal.NvidiaGpu()
        self.drives = thermal.Drives()
        self.lhm = thermal.LhmCpu()
        self._cpu_read = (0.0, None)          # (when, CPU die temperature from LibreHardwareMonitor)
        self._holds: dict[int, tuple] = {}    # queued tasks waiting for their series or the task they go after
        self._swings: dict[int, dict | None] = {}  # running tasks whose CPU use goes up and down
        self._swing_at = 0.0
        self._cond: dict[int, dict] = {}       # what each task ran under (RMQ_CONDITIONS_FILE)
        self._cond_written = 0.0
        self._phist: dict[int, deque] = {}     # (running time, progress %) when the progress changed
        self.guard = thermal.ThermalGuard()
        self.regulator = thermal.HeatRegulator()
        self._heat_state = "normal"           # normal | warm (the regulator holds tasks back) | hot (frozen)
        self._reg_on = False
        self._reg_full_since: float | None = None
        self._gpu_read = (0.0, None, False)   # (when, temperature, throttling)
        self._ssd_read = (0.0, None, None)    # (when, temperature, drive's warning limit)
        self._clock: tuple[float, float] | None = None  # (wall, unbiased) of the previous tick
        self._progress_cache: dict[int, tuple] = {}
        self._activity: dict[int, dict] = {}  # when each task last showed signs of work
        self._io_seen: dict[int, tuple[str, int, int]] = {}  # (job, bytes read, bytes written) at the last tick
        self._agent_names: dict[int, str] = {}
        self._disk_low: dict[str, bool] = {}
        self._disk_info: list[dict] = []
        self._disk_checked = 0.0
        self._drives: dict[str, str] = {}
        self.stats = StatsBook(home / "stats")
        # the charts' history survives restarts and updates; after a crash the day comes back from data/stats
        self._metrics_path = home / "metrics.json.gz"
        last = self.metrics.load(self._metrics_path, time.time())
        self.metrics.seed(self.stats.minutes_since(max(last, time.time() - 86400) + 59), time.time())
        if self.metrics.long_missing(time.time()):  # the month's charts: what is not in the cache, from data/stats
            self.metrics.seed_long(self.stats.minutes_since(time.time() - self.metrics.long_s), time.time())
        self._metrics_saved = time.time()
        # the compute network: tasks other PCs run here live under LocalLow (they may write nowhere else)
        self.netstore = NetStore(home / "network.json")
        self.net_root = Path(winapi.local_low()) / "RMQ" / self.instance
        # tasks other PCs run here -> the network they came over ("home": one's own PC, "ext": other people)
        self.hosted: dict[int, str] = {r["id"]: (r["origin_net"] or "ext") for r in self.db.tasks_hosted()}
        self.net = NetService(self)
        self._hw: dict | None = None
        self._net_disk = (0.0, 0.0)  # (when, GB used by members' files)
        self.open_dashboard_once = False   # set by the updater: the server opens the dashboard when it starts
        self._dashboard_seen = bool(self.db.kv_get("dashboard_seen"))
        self.updater = Updater(self)
        self.restart_cb = None  # set by the server: stops it, so that an update can be installed

    # ================================================================ setup
    def task_dir(self, tid: int) -> Path:
        """A task's log, exit code, progress and stop file. Members' tasks keep them under LocalLow."""
        return (self.net_root / "tasks" / str(tid)) if tid in self.hosted else (self.tasks_dir / str(tid))

    def dashboard_seen(self) -> None:
        """The owner's dashboard loaded and asked for the state: remembered once."""
        if not self._dashboard_seen:
            self._dashboard_seen = True
            self.db.kv_set("dashboard_seen", str(int(time.time())))

    def net_dir(self, name: str) -> Path:
        """Folders of the network: what members' tasks use lives under LocalLow, the rest in data/net."""
        base = self.net_root if name in ("tasks", "work", "envs", "cache") else self.home / "net"
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _admin_token(self) -> str:
        path = self.home / "admin_token.txt"
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = "adm_" + secrets.token_urlsafe(24)
        path.write_text(token, encoding="utf-8")
        return token

    def _ensure_user_agent(self) -> int:
        row = self.db.agent_by_name("user")
        if row:
            return row["id"]
        a = self.settings.section("agents")
        row, _ = self.db.agent_create("user", weight=a["default_weight"], max_priority=2,
                                      max_running=max(8, a["default_max_running"]),
                                      max_queued=a["default_max_queued"], builtin=True,
                                      note="Задачи, поставленные из дашборда")
        return row["id"]

    def start(self) -> None:
        self._backfill_task_log()  # before any task can end and start the file itself
        with self.lock:
            self._adopt()
        self._thread = threading.Thread(target=self._loop, name="rmq-scheduler", daemon=True)
        self._thread.start()
        self.net.start()

    def stop(self) -> None:
        self.net.stop()
        self._stop.set()
        self.wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.keep_awake.close()
        self.nv.close()
        self.stats.close()
        try:
            self.metrics.save(self._metrics_path)
        except OSError as e:
            log.warning("could not save the charts' history: %s", e)
        with self.lock:
            self._flush(force=True)
            for rt in list(self.runtimes.values()) + list(self.draining.values()):
                rt.close()  # tasks keep running; the next start re-attaches to them
            self.runtimes.clear()

    def _adopt(self) -> None:
        for row in self.db.tasks_active():
            if row["state"] == "queued":
                continue
            rt = TaskRuntime.adopt(row["id"], row["job_name"]) if row["job_name"] else None
            if rt is not None:
                rt.suspended_pids = set(json.loads(row["suspended_pids"] or "[]"))
                self.runtimes[row["id"]] = rt
                log.info("re-attached to task #%d", row["id"])
                self.db.event("adopt", "Менеджер снова управляет задачей после перезапуска",
                              task_id=row["id"], agent_id=row["agent_id"])
            else:
                self._job_vanished(row)

    def _job_vanished(self, row: dict) -> None:
        """The task's job no longer exists: it finished while we were away, or never got going."""
        if self._stopping(row):  # it saved a checkpoint and exited, as it was asked to
            self._kill_stray(row)
            self._requeue_row(row, row["state_reason"][9:], time.time(), graceful=True)
            return
        info = self._read_exit(row["id"])
        if info is not None:
            self._finalize(row, "done" if info["code"] == 0 else "failed", info["code"],
                           info.get("error") or "", when=info.get("ts"))
            return
        self._kill_stray(row)
        if row["preempt"] == "requeue":
            self._requeue_row(row, "lost", time.time())
        else:
            self._finalize(row, "lost", None, "Процесс задачи пропал (менеджер был остановлен или его убили извне)")

    def _kill_stray(self, row: dict) -> None:
        """Our own wrapper that never got into its job (crash mid-launch): identified by pid + start time."""
        if not row["pid"] or not row["pid_ctime"]:
            return
        try:
            p = psutil.Process(row["pid"])
            if abs(p.create_time() - row["pid_ctime"]) < 0.01:
                for c in p.children(recursive=True):
                    c.kill()
                p.kill()
        except psutil.Error:
            pass

    # ================================================================ loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception:  # keep scheduling no matter what
                log.exception("tick failed")
            period = self.settings.section("scheduler")["tick_s"]
            self.wake.wait(max(0.05, period - (time.monotonic() - t0)))
            self.wake.clear()

    def _window(self, name: str, size: float, mean: bool = False):
        kind = planner.WindowMean if mean else planner.WindowMax
        w = self._win.get(name)
        if not isinstance(w, kind) or w.window_s != size:
            w = self._win[name] = kind(size)
        return w

    def tick(self) -> None:
        S = self.settings.snapshot()
        sample = self.sampler.sample()
        gpu = sample.gpu
        now = sample.t
        with self.lock:
            dt = 0.0 if self._last_tick is None else min(5.0, max(0.0, now - self._last_tick))
            self._last_tick = now
            self._account_sleep(now)
            rows = {r["id"]: r for r in self.db.tasks_active()}

            # 1. observe our tasks
            usage: dict[int, Usage] = {}
            managed: set[int] = set()
            for tid, row in list(rows.items()):
                if row["state"] == "queued":
                    continue
                rt = self.runtimes.get(tid)
                if rt is None and row["job_name"]:
                    rt = TaskRuntime.adopt(tid, row["job_name"])
                    if rt is not None:
                        self.runtimes[tid] = rt
                if rt is None:
                    self._job_vanished(row)
                    rows.pop(tid)
                    continue
                try:
                    u = rt.sample(now)
                except OSError:
                    log.exception("cannot sample task #%d", tid)
                    continue
                info = self._read_exit(tid)
                if info is not None or u.active == 0:
                    self._task_ended(row, rt, info, u, now)
                    rows.pop(tid)
                    continue
                usage[tid] = u
                managed.update(u.pids)
            self._drain(now, S)
            for tid in set(self._activity) - set(usage):
                del self._activity[tid]

            task_vram = {tid: (gpu.vram_of(u.pids) / GB if gpu else 0.0) for tid, u in usage.items()}
            task_gpu = {tid: (gpu.util(set(u.pids)) if gpu else 0.0) for tid, u in usage.items()}
            for tid, u in usage.items():
                lv = self._live(rows[tid])
                lv["peak_ram_gb"] = max(lv["peak_ram_gb"], u.ws / GB)
                lv["peak_vram_gb"] = max(lv["peak_vram_gb"], task_vram[tid])
                if task_vram[tid] > 0.05 or task_gpu[tid] > 1.0:
                    lv["uses_gpu"] = 1
                self._watch_activity(rows[tid], self.runtimes[tid], u, task_gpu[tid], now,
                                     S["tasks"]["hung_after_min"] * 60)

            # 2. what the user (everything not ours) uses right now
            N = self.n_cpu
            ram_total = sample.ram_total / GB
            ram_avail = sample.ram_available / GB
            tasks_cpu = sum(u.cpu for u in usage.values())
            tasks_ram = sum(u.ws for u in usage.values()) / GB
            cpu_user = max(0.0, sample.cpu_frac * N - tasks_cpu)
            ram_user = max(0.0, ram_total - ram_avail - tasks_ram)
            vram_total = self.sampler.gpu.total / GB if self.sampler.gpu else 0.0
            vram_used = gpu.used / GB if gpu else 0.0
            tasks_vram = sum(task_vram.values())
            vram_user = max(0.0, vram_used - tasks_vram)
            gpu_user = gpu.user_util(managed) if gpu else 0.0
            gpu_tasks = gpu.util(managed) if gpu and managed else 0.0

            # temperatures: the regulator trims the tasks' CPU to hold every component at its target;
            # past a hot limit everything is frozen until all of it has cooled down
            tc = dict(S["thermal"])
            if self.nv.max_operating:
                tc["gpu_hot_c"] = min(tc["gpu_hot_c"], self.nv.max_operating - 2)
            readings = self._read_sensors(now, tc, max(gpu_user, gpu_tasks), bool(usage))
            was_hot = self.guard.state == "hot"
            hot = self.guard.update(now, readings, tc) == "hot"
            factor = self.regulator.update(now, readings, tc)
            if was_hot and not hot:  # back on part of the CPU, the rest comes back gradually
                self.regulator.after_freeze()
                factor = self.regulator.factor
            heat = "hot" if hot else "warm" if self._regulating(now, factor) else "normal"
            if heat != self._heat_state:
                self._heat_event(self._heat_state, heat, readings)
                self._heat_state = heat

            # 3. game mode, smoothing, GPU yield
            gm, sm, rs, g = S["game_mode"], S["smoothing"], S["reserve"], S["gpu"]
            auto_game, game_src = False, None
            if self.mode == "auto" and gm["auto_detect"]:
                auto_game, game_src = self.games.poll(now, gm, managed)
            game_active = self.mode == "game" or auto_game
            extra_ram = extra_vram = 0.0
            learned = None
            if game_active:
                extra_ram = gm["extra_ram_reserve_gb"]
                if auto_game and self.games.key and gm["learn_game_memory"]:
                    g_ram, g_vram = self._game_usage(gpu)
                    learned = self._learn_game(self.games.key, g_ram, g_vram, now)
                    if learned and learned["ram"] > 0.3:  # free in advance what the game took last time
                        extra_ram = max(0.0, learned["ram"] - g_ram)
                        extra_vram = max(0.0, learned["vram"] - g_vram)
            # CPU: the owner's average load (priority already lets the owner preempt tasks instantly);
            # RAM/VRAM: the owner's peak, because memory cannot be taken back that fast
            cpu_user_s = self._window("cpu", sm["cpu_window_s"], mean=True).push(now, cpu_user)
            ram_user_s = self._window("ram", sm["ram_window_s"]).push(now, ram_user)
            vram_user_s = self._window("vram", sm["vram_window_s"]).push(now, vram_user)
            self.gpu_busy.on_s, self.gpu_busy.off_s = g["busy_confirm_s"], g["idle_confirm_s"]
            gpu_busy = self.gpu_busy.update(now, gpu_user > g["user_busy_pct"])

            # 4. pools: what tasks may use in total
            pool_cpu, res_cpu = planner.pool_amount(N, cpu_user_s, rs["cpu_pct_of_free"], rs["cpu_min_cores"])
            use_game_cores = bool(game_active and gm["cpu_cores"] == "efficiency" and self.game_mask)
            if use_game_cores:
                pool_cpu = min(pool_cpu, float(self.n_game_cores))
            self.mask = self.game_mask if use_game_cores else self.all_mask
            pool_cpu *= factor
            pool_ram, res_ram = planner.pool_amount(ram_total, ram_user_s, rs["ram_pct_of_free"],
                                                    rs["ram_min_gb"], extra_ram)
            pool_vram = res_vram = 0.0
            if vram_total:
                pool_vram, res_vram = planner.pool_amount(vram_total, vram_user_s, rs["vram_pct_of_free"],
                                                          rs["vram_min_gb"], extra_vram)

            # 5. instantaneous pressure
            reserve_now = planner.reserve_amount(max(0.0, ram_total - ram_user), rs["ram_pct_of_free"],
                                                 rs["ram_min_gb"]) + extra_ram
            vram_deficit = 0.0
            if vram_total:
                vres_now = planner.reserve_amount(max(0.0, vram_total - vram_user), rs["vram_pct_of_free"],
                                                  rs["vram_min_gb"]) + extra_vram
                vram_deficit = vres_now - (vram_total - vram_used)
            commit_total, commit_limit = sample.commit_total / GB, sample.commit_limit / GB
            commit_excess = commit_total - commit_limit * rs["commit_critical_pct"] / 100

            # 6. plan
            pol = planner.Policy.from_settings(S)
            agents_rows = {a["id"]: a for a in self.db.agents()}
            agents = {aid: planner.AgentView(aid, a["name"], a["weight"], a["max_running"], bool(a["enabled"]),
                                             self.agent_usage.get(aid, 0.0)) for aid, a in agents_rows.items()}
            self._holds, doomed = self._task_holds(rows)
            for row, why in doomed:
                self._finalize(row, "cancelled", None, why)
                rows.pop(row["id"], None)
            views = {tid: self._view(row, usage.get(tid), task_vram.get(tid, 0.0), now) for tid, row in rows.items()}
            disk_hold = self._disk_guard(now, rows, S)
            plan = planner.plan(planner.PlanInput(
                now=now, capacity=planner.Capacity(N, ram_total, vram_total),
                pools=planner.Pools(pool_cpu, pool_ram, pool_vram), tasks=list(views.values()), agents=agents,
                policy=pol, mode=self.mode, game_active=game_active, gpu_user_busy=gpu_busy,
                thermal_freeze=heat == "hot", disk_hold=disk_hold,
                net_hold={tid for tid in rows if self.hosted.get(tid) == "ext"}  # one's own PCs' tasks never wait
                if self.netstore.mode != "two_way" else set(),
                hung={tid for tid, a in self._activity.items() if a["hung"] and tid in rows
                      and rows[tid]["state"] == "running"},
                ram_deficit=max(0.0, reserve_now - ram_avail), ram_critical=ram_avail < reserve_now * 0.5,
                vram_deficit=max(0.0, vram_deficit), commit_total=commit_total, commit_limit=commit_limit,
                commit_excess=max(0.0, commit_excess), last_shed_at=self.last_shed_at))
            self.plan = plan

            held = dict.fromkeys(planner.RESOURCES, 0.0)
            for v in views.values():
                if v.state == "running":
                    for r, x in planner.reserved(v).items():
                        held[r] += x

            # 7. act
            for a in plan.actions:
                try:
                    self._act(a, rows[a.task_id], usage.get(a.task_id), now, S)
                except Exception:
                    log.exception("action %s on task #%d failed", a.kind, a.task_id)
            if plan.shed:
                self.last_shed_at = now
            pw = S["power"]
            battery, battery_pct = winapi.on_battery()
            active_rows = self.db.tasks_active()
            running_now = any(r["state"] == "running" for r in active_rows) or (
                heat == "hot" and any(r["state_reason"] == "heat" for r in active_rows))  # cool down, then go on
            want_awake = bool(running_now and pw["keep_awake_while_running"]
                              and (not battery or pw["keep_awake_on_battery"]))
            if want_awake != self.keep_awake.active:
                self.keep_awake.set(want_awake)
                self.db.event("power", "ПК не уйдёт в сон, пока идут расчёты (экран может гаснуть)" if want_awake
                              else "Расчётов нет — сон снова разрешён" if not running_now
                              else "Работа от батареи — сон разрешён, чтобы не разрядить её")
            eco = bool(game_active and gm["eco_qos"])
            for tid, rt in self.runtimes.items():
                rt.low_io = S["tasks"]["low_io_priority"]
                try:
                    rt.set_limits(self.mask, None)
                    rt.set_eco(eco)
                    if tid in plan.cpu_caps:
                        rt.set_cpu_cap(plan.cpu_caps[tid], N)
                except OSError:
                    log.exception("cannot apply limits to task #%d", tid)

            # 8. fair-share accounting
            if dt > 0:
                decay = 0.5 ** (dt / max(60.0, S["scheduler"]["usage_half_life_h"] * 3600))
                for aid in self.agent_usage:
                    self.agent_usage[aid] *= decay
                for tid, v in views.items():
                    if v.state != "running" or tid not in usage:
                        continue
                    res = planner.reserved(v)
                    d = max(res["cpu"] / N, res["ram"] / ram_total, res["vram"] / vram_total if vram_total else 0)
                    if planner.is_overtime(v, pol):
                        d *= S["scheduler"]["overtime_usage_mult"]
                    self.agent_usage[v.agent_id] = self.agent_usage.get(v.agent_id, 0.0) + d * dt
                # time a task ran on less CPU than it asked for (the owner's game, the regulator, sharing, the soft
                # start): it does not count as running longer than declared
                for tid, cap in plan.cpu_caps.items():
                    v, lv = views.get(tid), self.live.get(tid)
                    if v is None or lv is None or v.state != "running" or v.cpu <= 0:
                        continue
                    short = 1.0 - min(1.0, cap / v.cpu)
                    if short > 0.02:
                        lv["held_s"] = (lv.get("held_s") or 0.0) + short * dt
                # what each task runs under, for timings it makes itself (RMQ_CONDITIONS_FILE)
                n_running = sum(1 for v in views.values() if v.state == "running")
                for tid, v in views.items():
                    if v.state not in ("running", "suspended") or tid not in rows:
                        continue
                    c = self._conds(rows[tid])
                    if v.state == "suspended":
                        c["paused_s"] += dt
                        continue
                    if use_game_cores:
                        c["ecores_s"] += dt
                    if game_active:
                        c["game_s"] += dt
                    if factor < 0.99:
                        c["throttled_s"] += dt
                    if n_running > 1:
                        c["shared_s"] += dt
                        c["max_neighbours"] = max(c["max_neighbours"], n_running - 1)
                    c["owner_core_s"] += cpu_user * dt
                    for k, x in self.lhm.extra.items():  # the CPU's power and clocks meanwhile
                        acc = c["sense"].setdefault(k, [0.0, 0.0])
                        acc[0] += x * dt
                        acc[1] += dt
                if now - self._cond_written >= 10:
                    self._cond_written = now
                    self._write_conditions(views, rows, now)
            self._flush()
            if now - self._metrics_saved >= 600:
                self._metrics_saved = now
                try:
                    self.metrics.save(self._metrics_path)
                except OSError as e:
                    log.warning("could not save the charts' history: %s", e)
            self._cleanup(now, S)
            self.updater.tick(now)

            # 9. publish
            n = {"running": 0, "queued": 0, "suspended": 0}
            for r in self.db.tasks_active():
                n[r["state"]] += 1
            mvals = {
                "cpu_user": cpu_user / N * 100, "cpu_tasks": tasks_cpu / N * 100,
                "cpu_limit": min(100.0, (cpu_user + pool_cpu) / N * 100),
                "ram_user": ram_user, "ram_tasks": tasks_ram, "ram_limit": min(ram_total, ram_user + pool_ram),
                "vram_user": vram_user, "vram_tasks": tasks_vram,
                "vram_limit": min(vram_total, vram_user + pool_vram),
                "gpu_user": gpu_user, "gpu_tasks": gpu_tasks,
                "running": n["running"], "queued": n["queued"], "suspended": n["suspended"],
                "game": 1.0 if game_active else 0.0,
                "temp_cpu": next((r.temp for r in readings if r.key == "cpu"), None),
                "temp_board": next((r.temp for r in readings if r.key == "board"), None),
                "temp_gpu": next((r.temp for r in readings if r.key == "gpu"), None),
                "temp_ssd": next((r.temp for r in readings if r.key == "ssd"), None),
                "heat": {"normal": 0.0, "warm": 1.0, "hot": 2.0}[heat],
                "cpu_factor": factor * 100,
                "cpu_power": self.lhm.extra.get("power"), "clock_p": self.lhm.extra.get("clock_p"),
                "clock_e": self.lhm.extra.get("clock_e"),
                "tasks": {tid: (u.cpu / N * 100, u.ws / GB, task_vram.get(tid, 0.0), task_gpu.get(tid, 0.0))
                          for tid, u in usage.items()},
            }
            self.metrics.add(now, mvals)
            if now - self._swing_at >= 30:
                self._swing_at = now
                m = self.metrics.series(600)
                self._swings = {tid: swing.from_metrics(m, tid, N) for tid, r in rows.items() if r["state"] == "running"}
            lim = self.regulator.limiting
            self.state = {
                "now": now, "version": __version__, "mode": self.mode,
                "game": {"active": game_active, "auto": auto_game, "source": game_src if auto_game else None,
                         "forced": self.mode == "game", "reserve_ram": extra_ram, "reserve_vram": extra_vram,
                         "learned": bool(learned and learned["ram"] > 0.3)},
                "gpu_busy": gpu_busy,
                "power": {"keep_awake": self.keep_awake.active, "on_battery": battery, "battery_pct": battery_pct},
                "thermal": {"enabled": tc["enabled"], "regulate": tc["regulate"], "state": heat,
                            "trigger": self.guard.trigger, "cool_left": self.guard.cool_left,
                            "cool_confirm_s": tc["cool_confirm_s"], "factor": factor,
                            "min_cpu_factor": tc["min_cpu_factor"],
                            "limiting": {"label": lim.label, "temp": lim.temp, "target": lim.target} if lim else None,
                            "cpu_source": self.lhm.status, "readings": [vars(r) for r in readings],
                            "cpu_power": self.lhm.extra.get("power"), "clock_p": self.lhm.extra.get("clock_p"),
                            "clock_e": self.lhm.extra.get("clock_e")},
                "disk": {"enabled": S["disk"]["enabled"], "min_free_gb": S["disk"]["min_free_gb"],
                         "drives": self._disk_info, "held": len(disk_hold)},
                "network": self.net_summary(),
                "update": {k: v for k, v in self.updater.status.items() if k != "release"},
                "_counts": dict(n),
                "system": {
                    "cpu": {"n": N, "ecores": self.n_ecores, "on_ecores": use_game_cores,
                            "game_cores": "E-ядра" if self.ecore_mask else "половина потоков",
                            "user": cpu_user / N * 100, "tasks": tasks_cpu / N * 100,
                            "pool_cores": pool_cpu, "reserve_cores": res_cpu, "held": held["cpu"]},
                    "ram": {"total": ram_total, "user": ram_user, "tasks": tasks_ram, "available": ram_avail,
                            "pool": pool_ram, "reserve": res_ram, "held": held["ram"]},
                    "vram": ({"name": self.sampler.gpu.adapter["name"], "total": vram_total, "user": vram_user,
                              "tasks": tasks_vram, "pool": pool_vram, "reserve": res_vram, "held": held["vram"]}
                             if self.sampler.gpu else None),
                    "gpu": {"user": gpu_user, "tasks": gpu_tasks, "threshold": g["user_busy_pct"]},
                    "commit": {"total": commit_total, "limit": commit_limit,
                               "max_pct": rs["commit_max_pct"], "critical_pct": rs["commit_critical_pct"]},
                },
                "_usage": {tid: (u, task_vram.get(tid, 0.0), task_gpu.get(tid, 0.0)) for tid, u in usage.items()},
            }

            # 10. long-term statistics (data/stats)
            try:
                self._stats_tick(now, dt, rows, usage, task_gpu, mvals, readings, factor, heat, disk_hold,
                                 game_active, bool(battery), n["running"] > 0, {
                                     "n_cpu": N, "cpu_tasks": tasks_cpu, "cpu_user": cpu_user,
                                     "ram_tasks": tasks_ram, "vram_tasks": tasks_vram, "gpu_tasks": gpu_tasks})
            except Exception:
                log.exception("statistics failed")

    def _read_sensors(self, now: float, tc: dict, gpu_load: float, tasks_active: bool) -> list:
        cpu = self.cpu_zones.read()
        # the GPU is asked only while it is awake anyway (busy, or still cooling down)
        when, g_temp, g_thr = self._gpu_read
        watch = gpu_load > 1.0 or (g_temp is not None and g_temp > tc["gpu_normal_c"] - 2)
        if not watch:
            self.nv.read(False)
            g_temp, g_thr = None, False
        elif now - when >= 2.0:
            g_temp, g_thr = self.nv.read(True)
            self._gpu_read = (now, g_temp, g_thr)
        # drives: every 30 s while computing or still warm, otherwise every 5 min
        when, s_temp, s_warn = self._ssd_read
        period = 30.0 if tasks_active or (s_temp is not None and s_temp > tc["ssd_normal_c"]) else 300.0
        if now - when >= period and self.drives.drives:
            s_temp, s_warn = self.drives.read()
            self._ssd_read = (now, s_temp, s_warn)
        when, c_temp = self._cpu_read
        if now - when >= 2.0:
            c_temp = self.lhm.read()
            self._cpu_read = (now, c_temp)
        return thermal.readings_from(tc, cpu, (g_temp, g_thr, self.nv.present), (s_temp, s_warn), c_temp)

    def _regulating(self, now: float, factor: float) -> bool:
        """Whether the regulator noticeably holds the tasks back (released after a minute at full CPU)."""
        if factor < 0.9:
            self._reg_on, self._reg_full_since = True, None
        elif self._reg_on:
            if factor < 0.999:
                self._reg_full_since = None
            elif self._reg_full_since is None:
                self._reg_full_since = now
            elif now - self._reg_full_since >= 60:
                self._reg_on = False
        return self._reg_on

    def _heat_event(self, prev: str, state: str, readings: list) -> None:
        temps = ", ".join(f"{r.label} {r.temp:.0f}°C" for r in readings if r.temp is not None)
        share = self.regulator.factor
        if state == "hot":
            msg = (f"Перегрев — {self.guard.trigger}. Задачи агентов заморожены до остывания, программы владельца "
                   f"работают как обычно ({temps})")
            self.stats.count("heat_freezes")
        elif prev == "hot":
            msg = f"ПК остыл ({temps}) — расчёты продолжаются" + (
                f", сначала на {share:.0%} процессора, остальное вернётся постепенно" if share < 0.999 else "")
        elif state == "warm":
            lim = self.regulator.limiting
            why = f"{lim.label} {lim.temp:.0f}°C при цели {lim.target:.0f}°C" if lim and lim.temp is not None else temps
            msg = f"Регулятор температуры урезает процессор задачам ({why}); сейчас им {share:.0%}"
        else:
            msg = f"Регулятор температуры вернул задачам весь процессор ({temps})"
        self.db.event("heat", msg)
        log.info(msg)

    # ---------------------------------------------------------------- disk space
    DISK_RESUME_EXTRA_GB = 2.0

    def _drive_of(self, path: str) -> str:
        """The root of the local drive a path is on ("C:\\"), or "" for a network share."""
        root = self._drives.get(path)
        if root is None:
            drive = os.path.splitdrive(os.path.abspath(path))[0]
            root = drive.upper() + "\\" if len(drive) == 2 and drive[1] == ":" else ""
            if root and not winapi.local_drive(root):
                root = ""
            if len(self._drives) > 1000:
                self._drives.clear()
            self._drives[path] = root
        return root

    def _disk_guard(self, now: float, rows: dict[int, dict], S: dict) -> set[int]:
        """Free space on the system drive and on the drives the tasks work on; returns the tasks that must
        stand still. A full system drive stops everything: Windows itself needs the room."""
        cfg = S["disk"]
        if not cfg["enabled"]:
            self._disk_info, self._disk_low = [], {}
            return set()
        system = (os.environ.get("SystemDrive") or "C:").upper()[:2] + "\\"
        by_root: dict[str, list[int]] = {}
        for tid, row in rows.items():
            by_root.setdefault(self._drive_of(row["cwd"]), []).append(tid)
        if now - self._disk_checked >= 2.0:
            self._disk_checked = now
            info = []
            for root in sorted({system, *(r for r in by_root if r)}, key=lambda r: (r != system, r)):
                try:
                    u = shutil.disk_usage(root)
                except OSError:
                    continue
                free = u.free / GB
                was = self._disk_low.get(root, False)
                low = free < cfg["min_free_gb"] + (self.DISK_RESUME_EXTRA_GB if was else 0.0)
                if low != was:
                    self._disk_event(root, low, free, cfg["min_free_gb"], root == system)
                self._disk_low[root] = low
                info.append({"drive": root[:2], "free": free, "total": u.total / GB, "system": root == system,
                             "low": low, "tasks": len(by_root.get(root, []))})
            self._disk_info = info
        if self._disk_low.get(system):
            return set(rows)
        return {tid for root, tids in by_root.items() if root and self._disk_low.get(root) for tid in tids}

    def _disk_event(self, root: str, low: bool, free: float, minimum: float, system: bool) -> None:
        if low:
            msg = (f"Мало места на диске {root[:2]} — свободно {free:.1f} ГБ при минимуме {minimum:g} ГБ: "
                   + ("все расчёты заморожены, пока место не освободится" if system
                      else "задачи, работающие на этом диске, заморожены, пока место не освободится"))
            self.stats.count("disk_freezes")
            log.warning(msg)
        else:
            msg = f"На диске {root[:2]} снова свободно {free:.1f} ГБ — расчёты продолжаются"
            log.info(msg)
        self.db.event("disk", msg)

    # ---------------------------------------------------------------- activity (a task that may hang)
    def _watch_activity(self, row: dict, rt: TaskRuntime, u: Usage, gpu_util: float, now: float,
                        hung_after_s: float) -> None:
        """When the task last showed signs of work: CPU, disk, log output, progress or GPU load."""
        tid = row["id"]
        a = self._activity.get(tid)
        if a is None or a["job"] != rt.job_name:
            a = self._activity[tid] = {"job": rt.job_name, "at": now, "check": 0.0, "cpu": u.cpu_s,
                                       "io": u.io_read + u.io_write, "log": None, "prog": None, "hung": False}
        if row["state"] != "running":  # frozen by the manager: not the task's fault, watch it afresh later
            a["at"], a["hung"] = now, False
        if now - a["check"] < 10:
            return
        a["check"] = now
        log_size, prog = self._log_size(tid), self._progress_mtime(tid)
        io = u.io_read + u.io_write
        busy = (u.cpu_s - a["cpu"] > 0.2 or io - a["io"] > 256 * 1024 or gpu_util > 2.0
                or (a["log"] is not None and log_size != a["log"]) or (a["prog"] is not None and prog != a["prog"]))
        a.update(cpu=u.cpu_s, io=io, log=log_size, prog=prog)
        if busy:
            a["at"], a["hung"] = now, False
        elif hung_after_s > 0 and not a["hung"] and now - a["at"] >= hung_after_s:
            a["hung"] = True
            self.db.event("hung", f"Зависла: {texts.fmt_minutes(now - a['at'])} не грузила процессор и видеокарту, "
                          "не писала в лог и на диск, прогресс не менялся", task_id=tid, agent_id=row["agent_id"])
            self.stats.count("hung_tasks")

    def _progress_mtime(self, tid: int) -> int | None:
        try:
            return (self.task_dir(tid) / "progress.txt").stat().st_mtime_ns
        except OSError:
            return None

    # ---------------------------------------------------------------- statistics
    def _agent_name(self, agent_id: int) -> str:
        name = self._agent_names.get(agent_id)
        if name is None:
            a = self.db.agent(agent_id)
            name = self._agent_names[agent_id] = a["name"] if a else f"#{agent_id}"
        return name

    def _stats_tick(self, now: float, dt: float, rows: dict, usage: dict, task_gpu: dict, mvals: dict,
                    readings: list, factor: float, heat: str, disk_hold: set, game: bool, battery: bool,
                    compute: bool, load: dict) -> None:
        per_agent: dict[str, dict] = {}
        io_r = io_w = 0
        for tid, u in usage.items():
            rt = self.runtimes.get(tid)
            seen = self._io_seen.get(tid)
            dr = dw = 0
            if rt is not None:
                if seen and seen[0] == rt.job_name:  # the same run: count what it did since the last tick
                    dr, dw = max(0, u.io_read - seen[1]), max(0, u.io_write - seen[2])
                self._io_seen[tid] = (rt.job_name, u.io_read, u.io_write)
            io_r += dr
            io_w += dw
            g = per_agent.setdefault(self._agent_name(rows[tid]["agent_id"]),
                                     {"tasks": 0, "cpu": 0.0, "gpu": 0.0, "ram": 0.0, "io_read": 0, "io_write": 0})
            g["tasks"] += rows[tid]["state"] == "running"
            g["cpu"] += u.cpu
            g["gpu"] += task_gpu.get(tid, 0.0)
            g["ram"] += u.ws / GB
            g["io_read"] += dr
            g["io_write"] += dw
        for tid in set(self._io_seen) - set(usage):
            del self._io_seen[tid]
        self.stats.add(now, dt, {
            **load, "compute": compute, "io_read": io_r, "io_write": io_w,
            "temps": [(r.key, r.temp, r.target) for r in readings], "share": factor,
            "regulating": heat == "warm", "heat_frozen": heat == "hot", "disk_frozen": bool(disk_hold),
            "disk_free": next((d["free"] for d in self._disk_info if d["system"]), None),
            "game": game, "paused": self.mode == "paused", "battery": battery,
            "agents": per_agent, "metrics": mvals})

    def get_chronicle(self, days: float) -> dict:
        """Every task alive during the last `days`, with its timeline rebuilt from the event log."""
        now = time.time()
        start = now - days * 86400
        rows = self.db.tasks_overlapping(start)
        by_task: dict[int, list] = {}
        for e in self.db.events_for_tasks([r["id"] for r in rows]):
            by_task.setdefault(e["task_id"], []).append(e)
        names = {a["id"]: a["name"] for a in self.db.agents()}
        tasks = []
        with self.lock:
            live = {tid: dict(lv) for tid, lv in self.live.items()}
        for r in rows:
            segs, marks = chronicle.segments(r, by_task.get(r["id"], []), now)
            segs = [s for s in segs if s[1] >= start]
            if not segs:
                continue
            lv = live.get(r["id"], {})
            tasks.append({
                "id": r["id"], "name": r["name"], "agent": names.get(r["agent_id"], "?"), "agent_id": r["agent_id"],
                "state": r["state"], "reason": r["state_reason"], "exit_code": r["exit_code"], "restarts": r["restarts"],
                "cpu": r["cpu"], "ram": r["ram_gb"], "vram": r["vram_gb"], "time_s": r["time_s"],
                "peak_ram": max(r["peak_ram_gb"], lv.get("peak_ram_gb", 0.0)),
                "peak_vram": max(r["peak_vram_gb"], lv.get("peak_vram_gb", 0.0)),
                "run_s": self.run_time(r, now), "cpu_s": r["cpu_s"], "submitted_at": r["submitted_at"],
                "finished_at": r["finished_at"], "segments": segs, "marks": [m for m in marks if m[0] >= start],
                "totals": chronicle.totals(segs), "where_node": r["remote_node"],
                "where": self.net.node_name(r["remote_node"]) if r["remote_node"] else None,
                "from": self.net.node_name(r["origin_node"]) if r["origin_node"] else None})
        tasks.sort(key=lambda t: (-t["segments"][0][0], -t["id"]))  # the newest on top
        agents = [{"id": a["id"], "name": a["name"], "builtin": bool(a["builtin"])} for a in self.db.agents()]
        return {"from": start, "to": now, "days": days, "tasks": tasks, "agents": agents}

    # ================================================================ the compute network
    def hardware(self) -> dict:
        if self._hw is None:
            gpu = self.sampler.gpu
            self._hw = {"cpu": cpu_model(), "threads": self.n_cpu, "ecores": self.n_ecores,
                        "ram_gb": round(psutil.virtual_memory().total / GB, 1),
                        "gpu": gpu.adapter["name"] if gpu else None, "vram_gb": round(gpu.total / GB, 1) if gpu else 0.0,
                        "os": f"Windows {platform.release()} ({platform.version()})",
                        "python": platform.python_version()}
        return self._hw

    def hosted_active(self, kind: str | None = None) -> list[int]:
        return [r["id"] for r in self.db.tasks_active()
                if r["id"] in self.hosted and (kind is None or self.hosted[r["id"]] == kind)]

    def net_summary(self) -> dict:
        st = self.netstore
        members = st.active_members("ext") if st.network else {}
        own = st.active_members("home") if st.home else {}
        return {"mode": st.mode, "status": self.net.status, "network": st.network["name"] if st.network else None,
                "members": len(members), "online": sum(1 for nid in members if self.net.online(nid)),
                "home": st.home["name"] if st.home else None, "home_members": len(own),
                "home_online": sum(1 for nid in own if self.net.online(nid)), "is_main": st.is_main,
                "hosted": len(self.hosted_active()), "remote": len(self.db.tasks_remote())}

    def nodes(self) -> list[dict]:
        """The PCs agents may send tasks to (rmq nodes): one's own PCs first, then other people's."""
        view = self.net.view()
        out, seen = [], set()
        for group, own in (((view["home"] or {}).get("members") or [], True), (view["members"], False)):
            for mbr in group:
                if mbr["left"] or mbr["node_id"] in seen:
                    continue
                seen.add(mbr["node_id"])
                info = mbr.get("info") or {}
                out.append({"name": mbr["name"], "self": mbr["self"], "own": own or bool(mbr.get("own")),
                            "main": bool(mbr.get("main")), "online": mbr["online"],
                            "mode": info.get("mode"), "accepting": bool(info.get("accepting")) and mbr["online"],
                            "hw": info.get("hw"), "free": info.get("free"), "busy": info.get("busy"),
                            "tasks": info.get("tasks"), "version": info.get("version"), "seen": mbr.get("seen")})
        return out

    def _fits_here(self, need: dict) -> bool:
        sysd = self.state.get("system") or {}
        cpu, ram, vram = sysd.get("cpu") or {}, sysd.get("ram") or {}, sysd.get("vram") or {}
        if any(r["state"] == "queued" for r in self.db.tasks_active()):
            return False
        free_cpu = cpu.get("pool_cores", 0) - cpu.get("held", 0)
        free_ram = ram.get("pool", 0) - ram.get("held", 0)
        free_vram = (vram.get("pool", 0) - vram.get("held", 0)) if vram else 0.0
        return free_cpu >= min(need["cpu"], cpu.get("pool_cores", 0)) and free_ram >= need["ram"] \
            and free_vram >= need["vram"]

    def _node_by_name(self, where: str) -> str | None:
        key = where.strip().lower()
        for nid, _kind in self.net.candidates():
            rec = self.netstore.home_members.get(nid) or self.netstore.members.get(nid) or {}
            if nid.lower() == key or str(rec.get("name") or "").lower() == key:
                return nid
        return None

    def _place(self, where, need: dict) -> str | None:
        """None: run here; otherwise the id of the PC that will run the task."""
        where = (str(where).strip() if where else "")
        if where.lower() in ("", "local", "here"):
            return None
        st = self.netstore
        if st.mode == "offline" or not (st.network or st.home):
            if where.lower() == "auto":
                return None
            raise ApiError(409, "сеть выключена или этот ПК не в сети: задачу можно поставить только сюда")
        if where.lower() == "auto":
            return None if self._fits_here(need) else self.net.best_node(need)
        if where.lower() == st.node_name.lower() or where == st.node_id:
            return None
        nid = self._node_by_name(where)
        if not nid:
            raise ApiError(404, f"в сети нет ПК «{where}»; список ПК: rmq nodes")
        name = self.net.node_name(nid)
        if not self.net.online(nid):
            raise ApiError(409, f"ПК {name} сейчас не в сети")
        info = self.net.info_of(nid)
        if not info.get("accepting"):
            raise ApiError(409, f"ПК {name} сейчас не принимает задачи: его владелец закрыл его для сети")
        hw = info.get("hw") or {}
        if need["cpu"] > hw.get("threads", 1e9) or need["ram"] > hw.get("ram_gb", 1e9) \
                or (need["vram"] > 0 and need["vram"] > (hw.get("vram_gb") or 0)):
            raise ApiError(400, f"на ПК {name} столько ресурсов нет: {hw.get('threads')} потоков, "
                                f"{hw.get('ram_gb')} ГБ ОЗУ, видеопамяти {hw.get('vram_gb') or 0} ГБ")
        return nid

    def _submit_remote(self, agent, body, node, argv, command, cwd, cpu, ram, vram, time_s, time_limit_s, prio,
                       preempt, warnings, series=None, after_id=None) -> dict:
        cfg = self.settings.section("network")
        files = body.get("files") or None
        results = body.get("results") or None
        req = body.get("requirements")
        for name, val in (("files", files), ("results", results)):
            if val is not None and not (isinstance(val, list) and all(isinstance(x, str) for x in val)):
                raise ApiError(400, f"{name}: список путей или шаблонов")
        if req is None and os.path.isfile(os.path.join(cwd, "requirements.txt")):
            req = "requirements.txt"
        if req:
            req = safe_name(str(req).replace("\\", "/"))
            if not req or not os.path.isfile(os.path.join(cwd, req)):
                raise ApiError(400, f"файл зависимостей не найден в папке задачи: {body.get('requirements')}")
        try:  # fail now, not in the background, when the project is too big
            listed = collect_files(Path(cwd), files, max_bytes=int(cfg["max_bundle_mb"]) << 20)
        except NetError as e:
            raise ApiError(400, e.message) from None
        extra = body.get("env_extra") or {}
        if not (isinstance(extra, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items())):
            raise ApiError(400, "env_extra: объект строка -> строка")
        name = (str(body.get("name") or "").strip() or default_name(argv or command.split()))[:200]
        where = self.net.node_name(node)
        with self.lock:
            now = time.time()
            tid = self.db.task_insert(
                agent_id=agent["id"], name=name, argv=json.dumps(argv) if argv else None, command=command, cwd=cwd,
                env=None, cpu=cpu, ram_gb=ram, vram_gb=vram, time_s=time_s, time_limit_s=time_limit_s, priority=prio,
                preempt=preempt, state="remote", state_reason="net:staging", submitted_at=now, queued_since=now,
                series=series, after_id=after_id, remote_node=node, remote_status=json.dumps({"phase_text": "готовим отправку"}, ensure_ascii=False),
                net_spec=json.dumps({"files": files, "results": results, "requirements": req, "env": extra,
                                     "agent": agent["name"], "net": self.net.channel(node)}, ensure_ascii=False))
            self.db.event("submit", f"Отправляется на ПК {where}: {name} ({len(listed)} файлов проекта)",
                          task_id=tid, agent_id=agent["id"])
        self.net.poke()
        warnings.append(f"задача выполнится на ПК {where}; файлы проекта ({len(listed)}) уедут туда, "
                        f"а созданные и изменённые задачей файлы вернутся в {cwd}")
        return {"id": tid, "state": "remote", "where": where, "warnings": warnings}

    @staticmethod
    def _remote_kind(row: dict) -> str:
        return json.loads(row["net_spec"] or "{}").get("net") or "ext"

    def _cancel_remote(self, row: dict, why: str, fetch: bool = True) -> bool:
        """Cancel a task sent to another PC. While that PC answers, the task ends there first and what it made so
        far comes back with its log (a checkpoint may be among it); otherwise it ends here at once and its files
        stay there. True: it is being cancelled there (still "remote" for now)."""
        node, rid = row["remote_node"], row["remote_id"]
        if rid and fetch and self.net.online(node):
            self.db.task_update(row["id"], state_reason="net:cancelling")
            self.net_remote_update(row["id"], {"phase_text": f"отменяется ({why}): заберём то, что она успела сделать",
                                                "cancel_why": why, "cancel_at": time.time()})
            self.net.pool.submit(self._send_cancel, node, rid, self._remote_kind(row), row["id"])
            return True
        if rid:
            self.net.pool.submit(self._send_cancel, node, rid, self._remote_kind(row))
        self._finalize(row, "cancelled", None, why)
        return False

    def net_cancel_gave_up(self, tid: int) -> None:
        """The other PC did not confirm the cancel: the task ends here; its files stay there."""
        with self.lock:
            row = self.db.task(tid)
            if not row or row["state"] != "remote":
                return
            rs = json.loads(row["remote_status"] or "{}")
            self._finalize(row, "cancelled", None, f"{rs.get('cancel_why') or 'отменена'}; что она успела сделать, "
                                                   f"осталось на ПК {self.net.node_name(row['remote_node'])}")

    def _send_cancel(self, node: str, rid: int, kind: str = "ext", tid: int | None = None) -> None:
        try:
            self.net.call(node, "POST", f"/net/v1/tasks/{rid}/cancel", {}, kind=kind, timeout=10)
        except PeerError as e:
            if tid is not None:  # nothing will come back: end it here
                self.net_cancel_gave_up(tid)
            if e.status != 404:
                pending = json.loads(self.db.kv_get("net_pending_cancels") or "[]")
                if [node, rid, kind] not in pending:
                    pending.append([node, rid, kind])
                    self.db.kv_set("net_pending_cancels", json.dumps(pending))

    def net_retry_cancels(self) -> None:
        pending = json.loads(self.db.kv_get("net_pending_cancels") or "[]")
        if not pending:
            return
        left = []
        for item in pending:
            node, rid, kind = (list(item) + ["ext"])[:3]
            if not self.netstore.net(kind) or node not in self.netstore.active_members(kind):
                continue
            if not self.net.online(node):
                left.append([node, rid, kind])
                continue
            try:
                self.net.call(node, "POST", f"/net/v1/tasks/{rid}/cancel", {}, kind=kind, timeout=10)
            except PeerError as e:
                if e.status != 404:
                    left.append([node, rid, kind])
        self.db.kv_set("net_pending_cancels", json.dumps(left))

    def _remote_log(self, row: dict, offset: int, limit: int) -> dict:
        if not row["remote_id"]:
            return {"data": "", "offset": 0, "size": 0, "rotated": False}
        try:
            return self.net.call(row["remote_node"], "GET", f"/net/v1/tasks/{row['remote_id']}/log?offset={offset}"
                                 f"&limit={max(1, min(limit, 1 << 20))}", kind=self._remote_kind(row), timeout=8)
        except PeerError as e:
            return {"data": f"[лог недоступен: {e.message}]\n" if offset <= 0 else "", "offset": max(0, offset),
                    "size": 0, "rotated": False}

    # ---------------------------------------------------------------- tasks this PC sent away (origin side)
    def net_remote_update(self, tid: int, patch: dict) -> None:
        with self.lock:
            row = self.db.task(tid)
            if not row or row["state"] != "remote":
                return
            cur = json.loads(row["remote_status"] or "{}")
            cur.update(patch)
            self.db.task_update(tid, remote_status=json.dumps(cur, ensure_ascii=False, default=str))

    def net_proxy_sent(self, tid: int, rid: int) -> None:
        with self.lock:
            row = self.db.task(tid)
            if not row or row["state"] != "remote":
                return
            self.db.task_update(tid, state_reason="net:sent", remote_id=rid, started_at=time.time())
            self.db.event("net", f"ПК {self.net.node_name(row['remote_node'])} принял задачу (там #{rid})",
                          task_id=tid, agent_id=row["agent_id"])

    def net_proxy_failed(self, tid: int, message: str) -> None:
        with self.lock:
            row = self.db.task(tid)
            if row and row["state"] == "remote":
                self._finalize(row, "failed", None, message)

    def net_proxy_finished(self, tid: int, st: dict, nfiles: int, nbytes: int) -> None:
        with self.lock:
            row = self.db.task(tid)
            if not row or row["state"] != "remote":
                return
            state = st.get("state") if st.get("state") in FINAL_STATES else "failed"
            cancel_why = json.loads(row["remote_status"] or "{}").get("cancel_why")
            self.db.task_update(tid, run_s=float(st.get("run_s") or 0.0), peak_ram_gb=float(st.get("peak_ram") or 0.0),
                                peak_vram_gb=float(st.get("peak_vram") or 0.0),
                                remote_status=json.dumps({"remote": st}, ensure_ascii=False, default=str))
            row = self.db.task(tid)
            where = self.net.node_name(row["remote_node"])
            note = f"выполнена на ПК {where}; вернулось файлов: {nfiles} ({nbytes / 2**20:.1f} МБ) в {row['cwd']}"
            if state == "cancelled" and cancel_why:
                note = f"{cancel_why}; {note}"
            elif state != "done" and st.get("reason_text"):
                note = f"{st['reason_text']}; {note}"
            self._finalize(row, state, st.get("exit_code"), note,
                           when=remote_end_time(st, row["submitted_at"], time.time()))

    def net_cancel_all(self, why: str, kind: str = "ext") -> None:
        """This PC leaves one of its networks: what it sent there and what it runs from there ends."""
        with self.lock:
            for row in self.db.tasks_remote():
                if self._remote_kind(row) == kind:
                    self._cancel_remote(row, why, fetch=False)
            for row in self.db.tasks_active():
                if self.hosted.get(row["id"]) == kind:
                    self._drop_runtime(row["id"], kill=True)
                    self._finalize(row, "cancelled", None, why)

    def net_member_left(self, node: str, kind: str = "ext") -> None:
        st = self.netstore
        with st._lock:
            rec = st.book(kind).get(node)
            if rec and not rec.get("left_at"):
                rec.update(left_at=time.time(), updated_at=time.time())
                st.save()
        name = self.net.node_name(node)
        where = "«Моих ПК»" if kind == "home" else "сети с друзьями"
        with self.lock:
            for row in self.db.tasks_active():
                if self.hosted.get(row["id"]) == kind and row["origin_node"] == node:
                    self._drop_runtime(row["id"], kill=True)
                    self._finalize(row, "cancelled", None, f"ПК {name} вышел из {where}")
            for row in self.db.tasks_remote():
                if row["remote_node"] == node and self._remote_kind(row) == kind:
                    self._finalize(row, "failed", None, f"ПК {name} вышел из {where}")
        self.db.event("net", f"ПК {name} вышел из {where}")

    # ---------------------------------------------------------------- tasks other PCs run here (host side)
    def _net_disk_gb(self) -> float:
        when, gb = self._net_disk
        if time.time() - when < 60:
            return gb
        total = 0
        for dirpath, _dirs, files in os.walk(self.net_root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        self._net_disk = (time.time(), total / GB)
        return total / GB

    def _net_agent(self, agent_name, node: str, kind: str = "ext") -> dict:
        cfg = self.settings.section("network")
        name = clean_agent_name(f"{agent_name or 'agent'} ({self.net.node_name(node)})")
        row = self.db.agent_by_name(name)
        if row is None:
            d = self.settings.section("agents")
            own = kind == "home"  # one's own PC: as one of the local agents
            try:
                row, _ = self.db.agent_create(name, weight=1.0 if own else cfg["remote_weight"],
                                              max_priority=1 if own else 0,
                                              max_running=d["default_max_running"], max_queued=d["default_max_queued"],
                                              note=f"задачи с моего ПК {self.net.node_name(node)}" if own
                                              else f"задачи из сети с ПК {self.net.node_name(node)}")
                self.agent_usage[row["id"]] = 0.0
                self.db.event("agent", f"Новый агент из сети: {name}", agent_id=row["id"])
            except sqlite3.IntegrityError:
                row = self.db.agent_by_name(name)
        if not row["enabled"]:
            raise NetError(f"владелец этого ПК отключил агента {name}", 403)
        return row

    def net_accept_task(self, node: str, spec: dict, kind: str = "ext") -> dict:
        """A member wants to run a task here: check it, then wait for its archive. One's own PCs are always let in;
        other people only while this PC is open to them, and only so many at once."""
        cfg = self.settings.section("network")
        if kind == "ext" and self.netstore.mode != "two_way":
            raise NetError("владелец этого ПК сейчас не принимает задачи из сети", 403)
        if kind == "ext" and len(self.hosted_active("ext")) >= cfg["max_hosted_tasks"]:
            raise NetError(f"на этом ПК уже {cfg['max_hosted_tasks']} задач из сети (его лимит): попробуйте позже", 409)
        bundle = spec.get("bundle") or {}
        size = int(bundle.get("size") or 0)
        if not (0 < size <= int(cfg["max_bundle_mb"]) << 20) or len(str(bundle.get("sha256") or "")) != 64:
            raise NetError(f"проект больше {cfg['max_bundle_mb']} МБ, которые принимает этот ПК", 413)
        if self._net_disk_gb() + size * 3 / GB > cfg["max_disk_gb"]:
            raise NetError("на этом ПК закончилось место, отведённое задачам из сети", 507)
        argv, command = spec.get("argv"), spec.get("command")
        if not ((isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv))
                or (isinstance(command, str) and command.strip())):
            raise NetError("нет команды задачи")
        try:
            cpu, ram, vram = float(spec["cpu"]), float(spec["ram"]), float(spec.get("vram") or 0)
            time_s = float(spec["time_s"])
        except (KeyError, TypeError, ValueError):
            raise NetError("неполная заявка задачи") from None
        hw = self.hardware()
        if not 0 < cpu <= self.n_cpu or ram > hw["ram_gb"] or (vram > 0 and vram > hw["vram_gb"]) or time_s <= 0:
            raise NetError(f"на этом ПК столько ресурсов нет: {self.n_cpu} потоков, {hw['ram_gb']} ГБ ОЗУ, "
                           f"видеопамяти {hw['vram_gb']} ГБ")
        upload_id = "u" + secrets.token_hex(8)
        with self.net._lock:
            self.net.uploads[upload_id] = {"node": node, "spec": spec, "size": size, "sha256": bundle["sha256"],
                                           "kind": kind, "expires": time.time() + 3600}
        return {"upload_id": upload_id}

    def net_finish_upload(self, node: str, upload_id: str, slot: dict, zip_path: Path) -> int:
        cfg = self.settings.section("network")
        spec = slot["spec"]
        work = self.net_dir("work") / upload_id
        try:
            manifest = safe_extract(zip_path, work, max_bytes=(int(cfg["max_bundle_mb"]) << 20) * 4)
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        req = spec.get("requirements")
        if req and safe_name(req) not in manifest:
            shutil.rmtree(work, ignore_errors=True)
            raise NetError("файл зависимостей не найден в архиве")
        kind = slot.get("kind", "ext")
        agent = self._net_agent(spec.get("agent"), node, kind)
        tl = spec.get("time_limit_s")
        with self.lock:
            now = time.time()
            tid = self.db.task_insert(
                agent_id=agent["id"], name=str(spec.get("name") or "task")[:200],
                argv=json.dumps(spec["argv"]) if spec.get("argv") else None, command=spec.get("command"),
                cwd=str(work), env=json.dumps(spec.get("env") or {}), cpu=float(spec["cpu"]), ram_gb=float(spec["ram"]),
                vram_gb=float(spec.get("vram") or 0), time_s=float(spec["time_s"]),
                time_limit_s=float(tl) if tl else None, priority=1 if kind == "home" else 0,
                preempt=spec.get("preempt") if spec.get("preempt") in ("suspend", "requeue") else "suspend",
                state="queued", submitted_at=now, queued_since=now, origin_node=node, origin_net=kind,
                origin_task=spec.get("origin_task"),
                net_spec=json.dumps({"requirements": req, "results": spec.get("results"), "upload": upload_id},
                                    ensure_ascii=False))
            self.hosted[tid] = kind
            self.task_dir(tid).mkdir(parents=True, exist_ok=True)
            (self.net_dir("manifests") / f"{tid}.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.db.event("submit", (f"Задача с моего ПК {self.net.node_name(node)}" if kind == "home"
                                     else f"Задача из сети с ПК {self.net.node_name(node)}") + f": {spec.get('name')}",
                          task_id=tid, agent_id=agent["id"])
        self.wake.set()
        return tid

    def _hosted_row(self, node: str, tid: int, kind: str = "ext") -> dict:
        """A hosted task, asked about by the PC that sent it and over the same network (a member of the network
        with other people can never reach a task one's own PC sent)."""
        row = self.db.task(tid)
        if not row or row["origin_node"] != node or (row["origin_net"] or "ext") != kind:
            raise NetError("нет такой задачи", 404)
        return row

    def net_hosted_status(self, node: str, tid: int, kind: str = "ext") -> dict:
        with self.lock:
            row = self._hosted_row(node, tid, kind)
            agents = {a["id"]: a for a in self.db.agents()}
            d = self.task_json(row, agents)
        keep = ("id", "state", "state_text", "reason", "reason_text", "wait_text", "position", "run_s", "progress",
                "used", "exit_code", "peak_ram", "peak_vram", "restarts", "cap_cores", "hung", "idle_s", "stopping",
                "finished_at", "started_at", "swing", "held_s", "conditions")
        out = {k: d.get(k) for k in keep}
        out["final"] = row["state"] in FINAL_STATES
        return out

    def net_hosted_log(self, node: str, tid: int, offset: int, limit: int, kind: str = "ext") -> dict:
        self._hosted_row(node, tid, kind)
        ident = Identity(True, self.db.agent(self.user_agent_id))
        return self.read_log(ident, tid, offset, limit)

    def net_hosted_log_all(self, node: str, tid: int, kind: str = "ext") -> bytes:
        self._hosted_row(node, tid, kind)
        path = self.task_dir(tid) / "output.log"
        try:
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                f.seek(max(0, size - (64 << 20)))
                return f.read()
        except OSError:
            return b""

    def net_hosted_cancel(self, node: str, tid: int, kind: str = "ext") -> dict:
        with self.lock:
            row = self._hosted_row(node, tid, kind)
            if row["state"] in ("queued", "running", "suspended"):
                self._drop_runtime(tid, kill=True)
                self._finalize(row, "cancelled", None, "отменена отправителем")
        self.wake.set()
        return {"id": tid, "state": "cancelled"}

    def net_hosted_results(self, node: str, tid: int, kind: str = "ext") -> Path:
        """The files the task created or changed, as one archive (built once)."""
        row = self._hosted_row(node, tid, kind)
        if row["state"] not in FINAL_STATES:
            raise NetError("задача ещё не закончилась", 409)
        out = self.net_dir("out") / f"results-{tid}.zip"
        if out.exists():
            return out
        cfg = self.settings.section("network")
        work = Path(row["cwd"])
        try:
            before = json.loads((self.net_dir("manifests") / f"{tid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            before = {}
        files, skipped = changed_files(work, before, int(cfg["max_results_mb"]) << 20) if work.exists() else ([], [])
        patterns = json.loads(row["net_spec"] or "{}").get("results")
        if patterns:
            files = [f for f in files if matches_any(f[0], patterns)]
        if skipped:
            self._log_note(tid, f"не вернутся (больше {cfg['max_results_mb']} МБ вместе): {', '.join(skipped[:20])}")
        make_zip(work, files, out)
        return out

    def net_hosted_delete(self, node: str, tid: int, kind: str = "ext") -> dict:
        row = self._hosted_row(node, tid, kind)
        if row["state"] not in FINAL_STATES:
            raise NetError("задача ещё не закончилась", 409)
        self._drop_hosted_files(row)
        return {"ok": True}

    def _drop_hosted_files(self, row: dict) -> None:
        tid = row["id"]
        if row["cwd"] and Path(row["cwd"]).resolve().is_relative_to(self.net_root.resolve()):
            shutil.rmtree(row["cwd"], ignore_errors=True)
        (self.net_dir("out") / f"results-{tid}.zip").unlink(missing_ok=True)
        (self.net_dir("manifests") / f"{tid}.json").unlink(missing_ok=True)

    # ---------------------------------------------------------------- statistics of one's other PCs too
    def owner(self) -> Identity:
        """The owner of this PC (the dashboard): what the main PC acts as on one of its PCs."""
        return Identity(True, self.db.agent(self.user_agent_id))

    def _own_pcs(self) -> list[tuple[str, str]]:
        st = self.net.store
        return [(nid, rec.get("name") or nid) for nid, rec in st.active_members("home").items()] if st.home else []

    def _gather(self, what: str, query: dict) -> tuple[list[tuple[str, str, dict]], list[str]]:
        """(node, name, answer) of each of one's other PCs that answered, and why the rest did not."""
        got, missing = [], []
        for nid, name in self._own_pcs():
            if not self.net.online(nid):
                missing.append(f"{name}: не в сети")
                continue
            try:
                got.append((nid, name, self.net.peek(nid, what, query)))
            except NetError as e:
                missing.append(f"{name}: {e.message}")
        return got, missing

    def stats_for(self, pc: str | None, period: str) -> dict:
        """This PC's statistics, another of one's PCs' (by name), or all of them together ("all")."""
        me = self.net.store
        pc = (pc or "").strip()
        if pc.lower() == "all":
            got, missing = self._gather("stats", {"period": period})
            return combine_reports([(me.node_name, self.get_stats(period))] + [(n, r) for _, n, r in got], missing)
        nid = self.net.find_home(pc) if pc else me.node_id
        if nid == me.node_id:
            return dict(self.get_stats(period), pc=me.node_name)
        return dict(self.net.peek(nid, "stats", {"period": period}), pc=self.net.node_name(nid), files=[])

    def chronicle_for(self, pc: str | None, days: float) -> dict:
        me = self.net.store
        pc = (pc or "").strip()
        if pc.lower() == "all":
            got, missing = self._gather("chronicle", {"days": days})
            return chronicle.merge([(me.node_id, True, me.node_name, self.get_chronicle(days))]
                                   + [(nid, False, n, ch) for nid, n, ch in got], missing)
        nid = self.net.find_home(pc) if pc else me.node_id
        if nid == me.node_id:
            return self.get_chronicle(days)
        out = self.net.peek(nid, "chronicle", {"days": days})
        for t in out.get("tasks") or []:
            t.update(node=nid, pc=self.net.node_name(nid))
        return out

    def check_updates_now(self) -> dict:
        """Look for an update right away (it installs itself if there is one)."""
        up = self.updater
        threading.Thread(target=up.check_and_install, name="rmq-update", daemon=True).start()
        return {k: v for k, v in up.status.items() if k != "release"}

    def get_stats(self, period: str) -> dict:
        if period not in ("24h", "7d", "30d", "all"):
            raise ApiError(400, "period: 24h, 7d, 30d или all")
        out = self.stats.report(period)
        tc = self.settings.section("thermal")
        out["targets"] = {c: tc[f"{c}_target_c"] for c in ("cpu", "board", "gpu", "ssd")}
        out["hot"] = {c: tc[f"{c}_hot_c"] for c in ("cpu", "board", "gpu", "ssd")}
        return out

    def _account_sleep(self, now: float) -> None:
        """If the PC slept since the last tick, that time is not counted as the tasks' running time."""
        unbiased = winapi.unbiased_seconds()
        if self._clock is not None:
            slept = (now - self._clock[0]) - (unbiased - self._clock[1])
            if slept > 5:
                n = 0
                span = (f"с {time.strftime('%d.%m %H:%M', time.localtime(now - slept))} "
                        f"до {time.strftime('%H:%M', time.localtime(now))} ({texts.fmt_minutes(slept)})")
                for row in self.db.tasks_active():
                    if row["state"] == "running" and row["last_start_at"]:
                        self.db.task_update(row["id"], last_start_at=row["last_start_at"] + slept)
                        self.db.event("power", f"ПК спал {span}: это время в работу задачи не засчитано",
                                      task_id=row["id"], agent_id=row["agent_id"])
                        self._conds(row)["slept_s"] += slept
                        n += 1
                self.db.event("power", f"ПК спал {texts.fmt_minutes(slept)}; это время не засчитано задачам ({n})")
        self._clock = (now, unbiased)

    def _game_usage(self, gpu) -> tuple[float, float]:
        ram = vram = 0.0
        for pid in self.games.pids:
            try:
                ram += psutil.Process(pid).memory_info().rss / GB
            except psutil.Error:
                continue
            if gpu:
                vram += gpu.proc_vram.get(pid, 0) / GB
        return ram, vram

    def _learn_game(self, key: str, ram: float, vram: float, now: float) -> dict:
        """Remember the most RAM/VRAM a game has ever taken; it is freed in advance at its next start."""
        p = self.game_profiles.get(key)
        known = dict(p) if p else None
        if p is None:
            p = self.game_profiles[key] = {"ram": 0.0, "vram": 0.0, "seen": now}
        if ram > p["ram"] + 0.05 or vram > p["vram"] + 0.05:
            p["ram"], p["vram"] = max(p["ram"], round(ram, 2)), max(p["vram"], round(vram, 2))
            self._profiles_dirty = True
        p["seen"] = now
        return known or p

    # ================================================================ task views and actions
    def _live(self, row: dict) -> dict:
        lv = self.live.get(row["id"])
        if lv is None:
            lv = self.live[row["id"]] = {k: row[k] for k in ("peak_ram_gb", "peak_vram_gb", "uses_gpu")}
            lv["held_s"] = row.get("held_s") or 0.0
            lv["cond"] = row.get("cond")
        return lv

    COND_KEYS = ("paused_s", "slept_s", "ecores_s", "game_s", "throttled_s", "shared_s", "owner_core_s")

    def _conds(self, row: dict) -> dict:
        c = self._cond.get(row["id"])
        if c is None:
            c = self._cond[row["id"]] = self._cond_of(row)
        return c

    def _cond_of(self, row: dict) -> dict:
        try:
            c = json.loads(row.get("cond") or "{}")
        except ValueError:
            c = {}
        for k in self.COND_KEYS:
            c[k] = float(c.get(k) or 0.0)
        c["max_neighbours"] = int(c.get("max_neighbours") or 0)
        sense = c.get("sense")  # {"power" | "clock_p" | "clock_e": [value x seconds, seconds measured]}
        c["sense"] = ({k: [float(v[0]), float(v[1])] for k, v in sense.items() if isinstance(v, list) and len(v) == 2}
                      if isinstance(sense, dict) else {})
        return c

    @staticmethod
    def _sensed(c: dict) -> dict:
        """The CPU's average package power and core clocks while the task ran (LibreHardwareMonitor); None where
        they were not measured."""
        s = c.get("sense") or {}
        avg = lambda k, d: round(s[k][0] / s[k][1], d) if k in s and s[k][1] > 0 else None
        return {"cpu_power_w": avg("power", 1), "clock_p_ghz": avg("clock_p", 2), "clock_e_ghz": avg("clock_e", 2)}

    def conditions(self, row: dict, now: float | None = None) -> dict:
        """What the task ran under since it first started: the time it was frozen, the PC slept, it got less CPU
        than it asked for, ran on the E-cores only (the owner's game), was held back by the temperature
        regulator, ran next to other tasks. All zero ("clean") means its own timings are not disturbed."""
        now = now or time.time()
        c = self._cond.get(row["id"]) or self._cond_of(row)
        lv = self.live.get(row["id"]) or {}
        run_s = self.run_time(row, now)
        end = row["finished_at"] or now
        out = {"task": row["id"], "updated": round(now, 1), "run_s": round(run_s, 1),
               "wall_s": round(end - row["started_at"], 1) if row["started_at"] else 0.0,
               "held_s": round(lv.get("held_s") or row.get("held_s") or 0.0, 1),
               **{k: round(c[k], 1) for k in self.COND_KEYS if k != "owner_core_s"},
               "max_neighbours": c["max_neighbours"],
               "owner_cpu_avg": round(c["owner_core_s"] / run_s, 2) if run_s > 0 else 0.0,
               **self._sensed(c), "cpu_claim": row["cpu"]}
        out["clean"] = all(out[k] < 1 for k in ("paused_s", "slept_s", "held_s", "ecores_s", "throttled_s", "shared_s"))
        return out

    def _write_conditions(self, views: dict, rows: dict, now: float) -> None:
        """Into each running task's RMQ_CONDITIONS_FILE (and saved with the task), every 10 s."""
        for tid, v in views.items():
            c, lv, row = self._cond.get(tid), self.live.get(tid), rows.get(tid)
            if c is None or row is None:
                continue
            if lv is not None:
                lv["cond"] = json.dumps(c)
            if v.state != "running":
                continue
            path = self.task_dir(tid) / "conditions.json"
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.conditions(row, now), ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        for tid in [t for t in self._cond if t not in rows]:  # ended: saved with it already
            self._cond.pop(tid, None)
            self._phist.pop(tid, None)

    @staticmethod
    def run_time(row: dict, now: float) -> float:
        """Closed running segments plus the current one (exact even across manager restarts)."""
        if row["state"] == "running" and row["last_start_at"]:
            return row["run_s"] + max(0.0, now - row["last_start_at"])
        return row["run_s"]

    def _view(self, row: dict, u: Usage | None, vram: float, now: float) -> planner.TaskView:
        lv = self._live(row) if row["state"] != "queued" else {
            k: row[k] for k in ("peak_ram_gb", "peak_vram_gb", "uses_gpu")}
        return planner.TaskView(
            id=row["id"], agent_id=row["agent_id"], state=row["state"], priority=row["priority"],
            cpu=row["cpu"], ram=row["ram_gb"], vram=row["vram_gb"], time_s=row["time_s"],
            preempt=row["preempt"], time_limit_s=row["time_limit_s"], queued_since=row["queued_since"],
            last_start_at=row["last_start_at"], run_s=self.run_time(row, now),
            started_before=row["started_at"] is not None,
            suspend_reason=row["state_reason"] if row["state"] == "suspended" else None,
            used_cpu=u.cpu if u else 0.0, used_ram=u.ws / GB if u else 0.0, used_vram=vram,
            commit=u.private / GB if u else 0.0, resume_ram=row["resume_ram_gb"],
            peak_ram=lv["peak_ram_gb"], peak_vram=lv["peak_vram_gb"], uses_gpu=bool(lv["uses_gpu"]),
            stopping=self._stopping(row), stop_reason=row["state_reason"][9:] if self._stopping(row) else "",
            stop_requested_at=(row["stop_requested_at"] or 0.0) if self._stopping(row) else None,
            held_s=lv.get("held_s") or 0.0, hold=self._holds.get(row["id"]) if row["state"] == "queued" else None)

    def _task_holds(self, rows: dict[int, dict]) -> tuple[dict[int, tuple], list[tuple[dict, str]]]:
        """Queued tasks that wait for their series (one task of a series at a time, oldest first) or for the task
        they go after; and those that can never start (that task ended without success)."""
        holds: dict[int, tuple] = {}
        doomed: list[tuple[dict, str]] = []
        first: dict[tuple, dict] = {}
        for r in sorted([r for r in list(rows.values()) + self.db.tasks_remote() if r.get("series")],
                        key=lambda r: r["id"]):
            first.setdefault((r["agent_id"], r["series"]), r)
        for r in rows.values():
            if r["state"] != "queued":
                continue
            if r.get("after_id"):
                p = self.db.task(r["after_id"])
                if p is None or (p["state"] in FINAL_STATES and p["state"] != "done"):
                    doomed.append((r, after_failed(r["after_id"], p)))
                    continue
                if p["state"] != "done":
                    holds[r["id"]] = ("after", {"for": p["id"]})
                    continue
            if r.get("series"):
                head = first.get((r["agent_id"], r["series"]))
                if head and head["id"] != r["id"]:
                    holds[r["id"]] = ("series", {"series": r["series"], "for": head["id"]})
        return holds, doomed

    def net_hold(self, row: dict) -> tuple[str, str] | None:
        """For a task waiting to be sent to another PC: ("wait", why) while its series or the task it goes after
        comes first, ("cancel", why) if it can never go; None to send it."""
        if row.get("after_id"):
            p = self.db.task(row["after_id"])
            if p is None or (p["state"] in FINAL_STATES and p["state"] != "done"):
                with self.lock:
                    cur = self.db.task(row["id"])
                    if cur and cur["state"] == "remote":
                        self._finalize(cur, "cancelled", None, after_failed(row["after_id"], p))
                return "cancel", ""
            if p["state"] != "done":
                return "wait", f"ждёт, пока успешно закончится задача #{p['id']}"
        if row.get("series"):
            older = [r["id"] for r in self.db.tasks_active() + self.db.tasks_remote()
                     if r.get("series") == row["series"] and r["agent_id"] == row["agent_id"] and r["id"] < row["id"]]
            if older:
                return "wait", f"серия «{row['series']}»: ждёт, пока закончится задача #{min(older)}"
        return None

    @staticmethod
    def _stopping(row: dict) -> bool:
        """Asked to save a checkpoint and exit (RMQ_STOP_FILE), and still running."""
        return row["state"] == "running" and (row["state_reason"] or "").startswith("stopping:")

    def _act(self, a: planner.Action, row: dict, u: Usage | None, now: float, S: dict) -> None:
        tid = row["id"]
        rt = self.runtimes.get(tid)
        why = texts.describe(a.reason)
        if a.kind == "start":
            self._start(row, now, S)
        elif a.kind == "resume" and rt:
            rt.resume()
            self.db.task_update(tid, state="running", state_reason="", last_start_at=now, suspended_pids="[]")
            stood = now - row["queued_since"] if row["queued_since"] else 0
            self.db.event("resume", f"Возобновлена после паузы {texts.fmt_minutes(stood)}" if stood >= 60
                          else "Возобновлена", task_id=tid, agent_id=row["agent_id"])
        elif a.kind == "suspend" and rt:
            rt.suspend(trim=a.reason in ("ram", "commit") or a.reason.startswith("fair:"))
            resume_ram = max(u.ws / GB if u else 0.0, row["resume_ram_gb"] if row["state"] == "suspended" else 0.0)
            self.db.task_update(tid, state="suspended", state_reason=a.reason, queued_since=now,
                                resume_ram_gb=resume_ram, suspended_pids=json.dumps(sorted(rt.suspended_pids)),
                                run_s=self.run_time(row, now))
            self.db.event("suspend", f"Пауза: {why}" if a.reason != "hung" else
                          f"Заморожена как зависшая. Продолжить: rmq resume {tid} или «Продолжить» в дашборде; "
                          f"отменить: rmq cancel {tid}", task_id=tid, agent_id=row["agent_id"])
        elif a.kind == "trim" and rt:
            rt.trim()
        elif a.kind == "stop" and rt:
            self._request_stop(row, a.reason, now, S)
        elif a.kind == "requeue":
            self._requeue_row(row, a.reason, now)
        elif a.kind == "kill":
            self._drop_runtime(tid, kill=True)
            self._finalize(row, "timeout", None, why)

    def _hosted_base_env(self, row: dict) -> dict[str, str]:
        """A member's task gets no personal variables of this PC, and every cache and temp folder inside its
        own folder (it may not write anywhere else)."""
        work = Path(row["cwd"])
        private = work / ".rmq"
        dirs = {"TEMP": "tmp", "TMP": "tmp", "APPDATA": "appdata/Roaming", "LOCALAPPDATA": "appdata/Local",
                "XDG_CACHE_HOME": "cache", "MPLCONFIGDIR": "cache/matplotlib", "NUMBA_CACHE_DIR": "cache/numba",
                "TORCH_HOME": "cache/torch", "HF_HOME": "cache/huggingface", "KERAS_HOME": "cache/keras",
                "CUPY_CACHE_DIR": "cache/cupy", "PYTHONPYCACHEPREFIX": "cache/pycache"}
        env = {k: os.environ[k] for k in HOSTED_ENV_KEEP if k in os.environ}
        for key, sub in dirs.items():
            d = private / sub
            d.mkdir(parents=True, exist_ok=True)
            env[key] = str(d)
        env["PIP_CACHE_DIR"] = str(self.net_dir("cache") / "pip")
        env["PYTHONNOUSERSITE"] = "1"
        for k, v in json.loads(row["env"] or "{}").items():  # what the sender asked for explicitly (--env)
            if str(k).upper() not in PROTECTED_ENV:
                env[str(k)] = str(v)
        return env

    def _task_env(self, row: dict, tdir: Path, S: dict) -> dict[str, str]:
        if row["id"] in self.hosted:
            base = self._hosted_base_env(row)
        else:
            base = json.loads(row["env"]) if row["env"] else dict(os.environ)
        env = {str(k).upper(): str(v) for k, v in base.items()}
        env.pop("__PYVENV_LAUNCHER__", None)
        cpu_n = max(1, math.ceil(row["cpu"]))
        env.update({
            "RMQ_TASK_ID": str(row["id"]), "RMQ_TASK_DIR": str(tdir), "RMQ_RESTART_COUNT": str(row["restarts"]),
            "RMQ_PROGRESS_FILE": str(tdir / "progress.txt"), "RMQ_STOP_FILE": str(tdir / "stop"),
            "RMQ_CONDITIONS_FILE": str(tdir / "conditions.json"),
            "RMQ_CPU": f"{row['cpu']:g}", "RMQ_RAM_GB": f"{row['ram_gb']:g}", "RMQ_VRAM_GB": f"{row['vram_gb']:g}",
            "RMQ_URL": f"http://{S['server']['host']}:{S['server']['port']}",
        })
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("MPLBACKEND", "Agg")  # plt.show() must never pop a window on the owner's screen
        if S["tasks"]["set_thread_env"]:
            for k in THREAD_VARS:
                env.setdefault(k, str(cpu_n))
        if row["vram_gb"] <= 0 and S["gpu"]["hide_gpu_for_cpu_tasks"]:
            env["CUDA_VISIBLE_DEVICES"] = ""
        elif row["vram_gb"] > 0:
            env.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")
            env.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
        return env

    def _start(self, row: dict, now: float, S: dict) -> None:
        tid = row["id"]
        if not os.path.isdir(row["cwd"]):
            self._finalize(row, "failed", None, f"Рабочая папка не найдена: {row['cwd']}")
            return
        tdir = self.task_dir(tid)
        tdir.mkdir(parents=True, exist_ok=True)
        for stale in ("exit.json", "progress.txt", "stop"):  # left over from a previous run of a requeued task
            (tdir / stale).unlink(missing_ok=True)
        self._progress_cache.pop(tid, None)
        self._activity.pop(tid, None)
        job_name = f"Local\\rmq-{self.instance}-task-{tid}-{row['restarts']}"
        spec = {"id": tid, "argv": json.loads(row["argv"]) if row["argv"] else None, "command": row["command"],
                "job_name": job_name, "log_max_bytes": int(S["tasks"]["log_max_mb"]) * 1024 * 1024}
        hosted = tid in self.hosted
        try:
            if hosted:
                req = json.loads(row["net_spec"] or "{}").get("requirements")
                if req:
                    path = Path(row["cwd"]) / req
                    h = requirements_hash(path.read_text(encoding="utf-8", errors="replace"), python_for_tasks())
                    spec["venv"] = {"dir": str(self.net_dir("envs") / h), "requirements": str(path),
                                    "python": python_for_tasks()}
            (tdir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            rt = TaskRuntime.launch(tid, job_name, [python_for_tasks(), "-I", "-X", "utf8", str(WRAPPER), str(tdir)],
                                    cwd=row["cwd"], env=self._task_env(row, tdir, S), affinity=self.mask,
                                    job_memory=None, low_io=S["tasks"]["low_io_priority"],
                                    low_integrity=self.hosted.get(tid) == "ext" and S["network"]["protect_host"])
        except Exception as e:  # bad env, missing interpreter, access denied...
            log.exception("cannot launch task #%d", tid)
            self._finalize(row, "failed", None, f"Не удалось запустить: {e}")
            return
        self.runtimes[tid] = rt
        ctime = None
        try:
            ctime = psutil.Process(rt.popen.pid).create_time()
        except psutil.Error:
            pass
        self.db.task_update(tid, state="running", state_reason="", started_at=row["started_at"] or now,
                            last_start_at=now, job_name=job_name, pid=rt.popen.pid, pid_ctime=ctime,
                            suspended_pids="[]")
        self.live[tid] = {"peak_ram_gb": row["peak_ram_gb"], "peak_vram_gb": row["peak_vram_gb"],
                          "uses_gpu": row["uses_gpu"]}
        restarted = f" (перезапуск №{row['restarts']})" if row["restarts"] else ""
        waited = now - row["queued_since"] if row["queued_since"] else 0
        self.db.event("start", f"Запущена{restarted}: {_request_text(row)}"
                      + (f"; ждала в очереди {texts.fmt_minutes(waited)}" if waited >= 60 else ""),
                      task_id=tid, agent_id=row["agent_id"])
        self.stats.count("tasks_started", self._agent_name(row["agent_id"]))

    def _request_stop(self, row: dict, reason: str, now: float, S: dict) -> None:
        """Ask a --preempt requeue task to save a checkpoint and exit; it keeps running until it does."""
        tid = row["id"]
        grace = S["tasks"]["stop_grace_s"]
        why = texts.describe(reason)
        try:
            (self.task_dir(tid) / "stop").write_text(
                f"{why}\nсохрани чекпоинт и заверши процесс в течение {grace:g} с\n", encoding="utf-8")
        except OSError:
            log.exception("cannot create the stop file of task #%d", tid)
        self.db.task_update(tid, state_reason=f"stopping:{reason}", stop_requested_at=now)
        self._log_note(tid, f"просьба сохранить чекпоинт и завершиться (появился файл RMQ_STOP_FILE): {why}; "
                            f"через {grace:g} с задача будет остановлена принудительно")
        self.db.event("stop", f"Попросили сохранить чекпоинт и завершиться (до {grace:g} с): {why}",
                      task_id=tid, agent_id=row["agent_id"])

    def _drop_runtime(self, tid: int, kill: bool) -> None:
        rt = self.runtimes.pop(tid, None)
        if rt:
            if kill:
                rt.kill()
            rt.close()

    def _requeue_row(self, row: dict, reason: str, now: float, graceful: bool = False) -> None:
        tid = row["id"]
        stopping = self._stopping(row)
        self._drop_runtime(tid, kill=True)
        lv = self.live.pop(tid, None) or {}
        self.db.task_update(
            tid, state="queued", state_reason=f"requeued:{reason}", queued_since=now, run_s=0.0,
            restarts=row["restarts"] + 1, job_name=None, pid=None, pid_ctime=None, suspended_pids=None,
            last_start_at=None, started_at=None, resume_ram_gb=0.0, stop_requested_at=None,
            peak_ram_gb=max(row["peak_ram_gb"], lv.get("peak_ram_gb", 0.0)),
            peak_vram_gb=max(row["peak_vram_gb"], lv.get("peak_vram_gb", 0.0)))
        (self.task_dir(tid) / "stop").unlink(missing_ok=True)
        why = texts.describe(reason)
        if stopping:
            how = "сохранила чекпоинт и завершилась по просьбе" if graceful else "не завершилась сама вовремя — остановлена"
            self.stats.count("stops_graceful" if graceful else "stops_forced")
            self._log_note(tid, f"{how}; будет запущена заново: {why}")
            self.db.event("requeue", f"{how[0].upper()}{how[1:]}; перезапуск позже: {why}", task_id=tid,
                          agent_id=row["agent_id"])
        else:
            self._log_note(tid, f"задача остановлена и будет запущена заново: {why}")
            self.db.event("requeue", f"Перезапуск позже: {why}", task_id=tid, agent_id=row["agent_id"])
        self.stats.count("tasks_requeued", self._agent_name(row["agent_id"]))

    def _task_ended(self, row: dict, rt: TaskRuntime, info: dict | None, u: Usage, now: float) -> None:
        tid = row["id"]
        if self._stopping(row):  # it saved a checkpoint and exited, as it was asked to
            self._requeue_row(row, row["state_reason"][9:], now, graceful=True)
            return
        if info is not None:
            code, note = info["code"], info.get("error") or ""
        else:
            code = rt.popen.poll() if rt.popen else None
            note = "Процесс задачи завершился без отчёта (возможно, его закрыли извне)"
        self.runtimes.pop(tid, None)
        if u.active > 0:  # leftover child processes get a short grace period, then are stopped
            rt.finished_at = now
            self.draining[tid] = rt
        else:
            rt.close()
        self._finalize(row, "done" if code == 0 else "failed", code, note)

    def _drain(self, now: float, S: dict) -> None:
        for tid, rt in list(self.draining.items()):
            try:
                alive = rt.sample(now).active
            except OSError:
                alive = 0
            if alive and now - (rt.finished_at or now) >= S["tasks"]["orphan_grace_s"]:
                rt.kill()
                alive = 0
            if not alive:
                rt.close()
                del self.draining[tid]

    def _finalize(self, row: dict, state: str, code: int | None, note: str, when: float | None = None) -> None:
        tid = row["id"]
        lv = self.live.pop(tid, None) or {}
        end = when or time.time()
        fields = dict(state=state, state_reason=note, exit_code=code, finished_at=end, suspended_pids=None,
                      run_s=self.run_time(row, end), stop_requested_at=None)
        for k in ("peak_ram_gb", "peak_vram_gb", "uses_gpu", "held_s"):
            if k in lv:
                fields[k] = lv[k]
        if tid in self._cond:  # what it ran under, up to the last tick
            fields["cond"] = json.dumps(self._cond[tid])
        self.db.task_update(tid, **fields)
        msg = {"done": "Завершена успешно", "failed": f"Завершилась с ошибкой (код {code})",
               "cancelled": "Отменена", "timeout": "Остановлена: превышен лимит времени",
               "lost": "Потеряна"}.get(state, state)
        if note and state not in ("done",):
            msg += f": {note}"
        if fields["run_s"]:
            peak = fields.get("peak_ram_gb", row["peak_ram_gb"]) or 0.0
            msg += f" · работала {texts.fmt_minutes(fields['run_s'])}" + (f", пик ОЗУ {peak:.1f} ГБ" if peak else "")
        self.db.event(state, msg, task_id=tid, agent_id=row["agent_id"])
        (self.task_dir(tid) / "stop").unlink(missing_ok=True)
        if not (row["remote_node"] and self._remote_kind(row) == "home"):  # one's own PC counts it where it ran
            kind = {"done": "tasks_done", "cancelled": "tasks_cancelled"}.get(state, "tasks_failed")
            self.stats.count(kind, self._agent_name(row["agent_id"]))
        final = self.db.task(tid)
        if final:
            self.stats.task_finished([self._task_record(final)])

    def _task_record(self, r: dict) -> dict:
        """The line of tasks.csv for a finished task."""
        stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
        c = self._cond.get(r["id"]) or self._cond_of(r)
        run = r["run_s"] or 0.0
        here = not r["remote_node"]
        started = r["started_at"]
        if not here:
            try:
                started = (json.loads(r["remote_status"] or "{}").get("remote") or {}).get("started_at") or started
            except ValueError:
                pass
        return {
            "finished": stamp(r["finished_at"]), "id": r["id"], "agent": self._agent_name(r["agent_id"]),
            "name": r["name"], "state": r["state"], "exit_code": r["exit_code"],
            "pc": self.net.store.node_name if here else self.net.node_name(r["remote_node"]),
            "from_pc": self.net.node_name(r["origin_node"]) if r["origin_node"] else "",
            "submitted": stamp(r["submitted_at"]), "started": stamp(started),
            "wait_min": (started - r["submitted_at"]) / 60 if started and r["submitted_at"] else None,
            "run_min": run / 60, "cpu_claim": r["cpu"], "ram_claim_gb": r["ram_gb"], "vram_claim_gb": r["vram_gb"],
            "time_claim_min": r["time_s"] / 60,
            "cpu_avg_cores": r["cpu_s"] / run if here and run > 0 and r["cpu_s"] else None,
            "peak_ram_gb": r["peak_ram_gb"], "peak_vram_gb": r["peak_vram_gb"], "restarts": r["restarts"],
            "held_min": (r.get("held_s") or 0.0) / 60, "paused_min": c["paused_s"] / 60, "slept_min": c["slept_s"] / 60,
            "ecores_min": c["ecores_s"] / 60, "throttled_min": c["throttled_s"] / 60, "shared_min": c["shared_s"] / 60,
            **self._sensed(c), "series": r.get("series") or "", "command": command_text(r),
        }

    def _backfill_task_log(self) -> None:
        """tasks.csv started with this version: the tasks still in the manager go in first."""
        if self.stats.tasks_path.exists():
            return
        rows = list(reversed(self.db.tasks_recent(1_000_000)))
        try:
            self.stats.task_finished([self._task_record(r) for r in rows])
        except Exception:
            log.exception("cannot fill tasks.csv")

    def _read_exit(self, tid: int) -> dict | None:
        path = self.task_dir(tid) / "exit.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _log_note(self, tid: int, text: str) -> None:
        path = self.task_dir(tid) / "output.log"
        if path.parent.exists():
            try:
                with open(path, "ab") as f:
                    f.write(f"[rmq {time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n".encode("utf-8"))
            except OSError:
                pass

    def _flush(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_flush >= 5:
            self._last_flush = now
            for tid, lv in self.live.items():
                rt = self.runtimes.get(tid)
                extra = {}
                if rt:
                    try:
                        extra["cpu_s"] = rt.cpu_seconds()
                    except OSError:
                        pass
                self.db.task_update(tid, **lv, **extra)
        if force or now - self._last_usage_flush >= 30:
            self._last_usage_flush = now
            for aid, usage in self.agent_usage.items():
                self.db.agent_update(aid, usage=usage)
            if self._profiles_dirty:
                self.db.kv_set("game_profiles", json.dumps(self.game_profiles, ensure_ascii=False))
                self._profiles_dirty = False

    def _cleanup(self, now: float, S: dict) -> None:
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        old = self.db.tasks_finished_before(now - S["tasks"]["keep_finished_days"] * 86400)
        for tid in old:
            shutil.rmtree(self.task_dir(tid), ignore_errors=True)
            self.hosted.pop(tid, None)
        if old:
            self.db.tasks_delete(old)
        self.db.events_prune(now - S["tasks"]["keep_finished_days"] * 86400)  # the journal: as long as the tasks
        keep = S["network"]["keep_results_h"] * 3600
        for row in self.db.tasks_hosted():  # results nobody came for
            if row["state"] in FINAL_STATES and (row["finished_at"] or now) < now - keep:
                self._drop_hosted_files(row)

    # ================================================================ API operations
    def identify(self, token: str | None, agent_name: str | None = None, agent_info: str = "") -> Identity | None:
        """The dashboard proves itself with the admin token; agents just say who they are.

        An agent is registered automatically the first time it calls. Agents never get the
        owner's rights: settings, modes, other agents' tasks and shutdown need the admin token.
        """
        if token:
            try:
                if secrets.compare_digest(token, self.admin_token):
                    return Identity(True, self.db.agent(self.user_agent_id))
            except TypeError:
                pass
            return None
        name = clean_agent_name(agent_name or "")
        if not name:
            return None
        if name == "user":
            raise ApiError(403, "имя user зарезервировано за владельцем ПК")
        row = self.db.agent_by_name(name)
        if row is None:
            row = self._register_agent(name, agent_info)
        if not row["enabled"]:
            raise ApiError(403, f"агент {name} отключён владельцем ПК")
        return Identity(False, row)

    def _register_agent(self, name: str, info: str) -> dict:
        d = self.settings.section("agents")
        try:
            row, _ = self.db.agent_create(
                name, weight=d["default_weight"], max_priority=parse_priority(d["default_max_priority"]),
                max_running=d["default_max_running"], max_queued=d["default_max_queued"],
                note=str(info or "")[:300])
        except sqlite3.IntegrityError:  # registered by a parallel request a moment ago
            return self.db.agent_by_name(name)
        self.agent_usage[row["id"]] = 0.0
        self.db.event("agent", f"Новый агент: {name}" + (f" ({info})" if info else ""), agent_id=row["id"])
        return row

    def _task_for(self, ident: Identity, tid: int, write: bool = False) -> dict:
        row = self.db.task(tid)
        if not row:
            raise ApiError(404, f"задача #{tid} не найдена")
        if write and not ident.admin and row["agent_id"] != ident.agent["id"]:
            raise ApiError(403, "это задача другого агента")
        return row

    def submit(self, ident: Identity, body: dict) -> dict:
        S = self.settings.snapshot()
        td = S["tasks"]
        agent = ident.agent
        argv, command = body.get("argv"), body.get("command")
        if command is not None:
            if not isinstance(command, str) or not command.strip():
                raise ApiError(400, "command должен быть непустой строкой")
            argv = None
        elif not (isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv) and argv[0]):
            raise ApiError(400, "нужен argv (список строк) или command (строка для cmd.exe)")
        cwd = body.get("cwd") or os.path.expanduser("~")
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            raise ApiError(400, f"рабочая папка не существует: {cwd}")
        env = body.get("env")
        if env is not None and not (isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                                  for k, v in env.items())):
            raise ApiError(400, "env должен быть объектом строка -> строка")
        try:
            cpu = float(body.get("cpu", td["default_cpu"]))
            ram = parse_size_gb(body.get("ram_gb", body.get("ram", td["default_ram_gb"])))
            vram = parse_size_gb(body.get("vram_gb", body.get("vram", td["default_vram_gb"])))
            time_s = parse_duration(body.get("time", td["default_time"]))
            limit = body.get("time_limit")
            time_limit_s = parse_duration(limit) if limit not in (None, "", 0) else None
            prio = parse_priority(body.get("priority", td["default_priority"]))
        except (TypeError, ValueError) as e:
            raise ApiError(400, str(e)) from e
        preempt = body.get("preempt", td["default_preempt"])
        if preempt not in ("suspend", "requeue"):
            raise ApiError(400, "preempt: suspend или requeue")
        cap = self.state.get("system", {})
        ram_total = psutil.virtual_memory().total / GB
        vram_total = self.sampler.gpu.total / GB if self.sampler.gpu else 0.0
        if body.get("where") not in (None, "", "local", "here"):  # checked against the PC that will run it
            ram_total, vram_total = 1e9, 1e9
        if not 0 < cpu <= self.n_cpu and body.get("where") in (None, "", "local", "here"):
            raise ApiError(400, f"cpu должно быть от 0.1 до {self.n_cpu}")
        if ram > ram_total:
            raise ApiError(400, f"ram больше, чем есть в ПК ({ram_total:.1f} ГБ)")
        if vram > 0 and vram > vram_total:
            raise ApiError(400, f"столько видеопамяти нет ({vram_total:.1f} ГБ)" if vram_total
                           else "видеокарта не найдена")
        if time_s <= 0:
            raise ApiError(400, "time должно быть больше нуля")
        warnings = []
        if prio > agent["max_priority"]:
            warnings.append(f"приоритет понижен до {PRIORITY_NAMES[agent['max_priority']]} (лимит агента)")
            prio = agent["max_priority"]
        warnings += self._claim_hints(agent, argv, command, cpu, ram, time_s)
        series = str(body.get("series") or "").strip()[:60] or None
        after_id = body.get("after")
        if after_id in (None, "", 0):
            after_id = None
        else:
            try:
                after_id = int(after_id)
            except (TypeError, ValueError):
                raise ApiError(400, "after: номер задачи") from None
            prev = self.db.task(after_id)
            if prev is None:
                raise ApiError(400, f"after: задачи #{after_id} нет")
            if prev["state"] in FINAL_STATES and prev["state"] != "done":
                raise ApiError(400, f"after: задача #{after_id} уже завершилась без успеха "
                                    f"({texts.STATE_NAMES.get(prev['state'], prev['state'])})")
        order = (f" · серия «{series}»" if series else "") + (f" · после #{after_id}" if after_id else "")
        node = self._place(body.get("where"), {"cpu": cpu, "ram": ram, "vram": vram})
        if node:
            return self._submit_remote(agent, body, node, argv, command, cwd, cpu, ram, vram, time_s, time_limit_s,
                                       prio, preempt, warnings, series, after_id)
        with self.lock:
            if self.db.count_queued(agent["id"]) >= agent["max_queued"]:
                raise ApiError(429, f"у агента уже {agent['max_queued']} задач в очереди (лимит)")
            pool = cap.get("ram", {})
            if pool and ram > pool.get("total", 0) - pool.get("user", 0) - pool.get("reserve", 0):
                warnings.append("сейчас столько свободной ОЗУ нет — задача подождёт, пока память освободится")
            name = (str(body.get("name") or "").strip() or default_name(argv or command.split()))[:200]
            now = time.time()
            tid = self.db.task_insert(
                agent_id=agent["id"], name=name, argv=json.dumps(argv) if argv else None, command=command,
                cwd=cwd, env=json.dumps(env) if env is not None else None, cpu=cpu, ram_gb=ram, vram_gb=vram,
                time_s=time_s, time_limit_s=time_limit_s, priority=prio, preempt=preempt, state="queued",
                submitted_at=now, queued_since=now, series=series, after_id=after_id)
            self.db.event("submit", f"Поставлена в очередь: {_request_text(dict(cpu=cpu, ram_gb=ram, vram_gb=vram, time_s=time_s))}{order}",
                          task_id=tid, agent_id=agent["id"])
        self.wake.set()
        return {"id": tid, "state": "queued", "warnings": warnings}

    def cancel(self, ident: Identity, tid: int) -> dict:
        with self.lock:
            row = self._task_for(ident, tid, write=True)
            if row["state"] == "remote":
                if self._cancel_remote(row, "отменена агентом" if not ident.admin else "отменена вручную"):
                    return {"id": tid, "state": "remote", "note": f"отменяется на ПК {self.net.node_name(row['remote_node'])}; "
                                                                  "файлы, которые она успела создать, и её лог вернутся сюда"}
                return {"id": tid, "state": "cancelled"}
            if row["state"] not in ("queued", "running", "suspended"):
                raise ApiError(409, f"задача уже завершена ({texts.STATE_NAMES.get(row['state'])})")
            self._drop_runtime(tid, kill=True)
            self._finalize(row, "cancelled", None, "отменена агентом" if not ident.admin else "отменена вручную")
        self.wake.set()
        return {"id": tid, "state": "cancelled"}

    def suspend_task(self, ident: Identity, tid: int) -> dict:
        with self.lock:
            row = self._task_for(ident, tid, write=True)
            if self._stopping(row):  # it is leaving anyway; frozen, it could never finish saving
                self._requeue_row(row, row["state_reason"][9:], time.time())
                self.wake.set()
                return {"id": tid, "state": "queued", "note": "задача уже сохранялась перед остановкой — "
                                                              "перезапущена позже"}
            if row["state"] == "running":
                rt = self.runtimes.get(tid)
                if rt:
                    u = rt.usage
                    rt.suspend(trim=False)
                    now = time.time()
                    self.db.task_update(tid, state="suspended", state_reason="user", queued_since=now,
                                        resume_ram_gb=u.ws / GB, suspended_pids=json.dumps(sorted(rt.suspended_pids)),
                                        run_s=self.run_time(row, now))
            elif row["state"] == "suspended":
                self.db.task_update(tid, state_reason="user")
            else:
                raise ApiError(409, "приостановить можно только выполняющуюся задачу")
            who = "владельцем ПК" if ident.admin else f"агентом {ident.agent['name']}"
            self.db.event("suspend", f"Приостановлена вручную ({who})", task_id=tid, agent_id=row["agent_id"])
        self.wake.set()
        return {"id": tid, "state": "suspended"}

    def resume_task(self, ident: Identity, tid: int) -> dict:
        with self.lock:
            row = self._task_for(ident, tid, write=True)
            if row["state"] != "suspended":
                raise ApiError(409, "задача не на паузе")
            self.db.task_update(tid, state_reason="resume")
        self.wake.set()
        return {"id": tid, "state": "suspended", "note": "возобновится, как только хватит ресурсов"}

    def set_priority(self, ident: Identity, tid: int, value) -> dict:
        try:
            prio = parse_priority(value)
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        with self.lock:
            row = self._task_for(ident, tid, write=True)
            if not ident.admin and prio > ident.agent["max_priority"]:
                raise ApiError(403, "выше лимита приоритета агента")
            self.db.task_update(tid, priority=prio)
        self.wake.set()
        return {"id": tid, "priority": PRIORITY_NAMES[prio]}

    def retime(self, ident: Identity, tid: int, value) -> dict:
        """The expected duration corrected while the task waits or runs (it learns its real length only then).
        Written to the journal, so the owner sees it."""
        try:
            time_s = parse_duration(value)
        except (TypeError, ValueError) as e:
            raise ApiError(400, str(e)) from e
        if time_s <= 0:
            raise ApiError(400, "time должно быть больше нуля")
        with self.lock:
            row = self._task_for(ident, tid, write=True)
            if row["state"] not in ("queued", "running", "suspended"):
                raise ApiError(409, "заявку времени можно поправить у задачи в очереди, на паузе или в работе на этом ПК")
            old = row["time_s"]
            self.db.task_update(tid, time_s=time_s)
            self.db.event("retime", f"Заявка времени изменена: ~{texts.fmt_minutes(old)} → ~{texts.fmt_minutes(time_s)} "
                                    f"({'вручную' if ident.admin else 'агентом'})", task_id=tid, agent_id=row["agent_id"])
        self.wake.set()
        return {"id": tid, "time_s": time_s, "old_time_s": old, "run_s": self.run_time(row, time.time())}

    def _claim_hints(self, agent: dict, argv, command, cpu: float, ram: float, time_s: float) -> list[str]:
        """What this agent's earlier runs of the same command (numbers aside) say about the request."""
        key = command_key(subprocess_list2cmdline(argv) if argv else command)
        past = [r for r in self.db.tasks_finished_by(agent["id"]) if command_key(command_text(r)) == key][:5]
        if not past:
            return []
        n = len(past)
        out = []
        peak = max(r["peak_ram_gb"] or 0.0 for r in past)
        if peak > 0.01:
            if ram > max(peak * 3, peak + 0.5):
                out.append(f"в прошлых запусках этой команды ({n}) пик ОЗУ был до {gb_text(peak)}, а заявлено "
                           f"{ram:g} ГБ: с завышенной заявкой задача дольше ждёт память (хватит ~{max(0.1, peak * 1.2):.1f} ГБ)")
            elif ram < peak * 0.85:
                out.append(f"в прошлых запусках этой команды ({n}) пик ОЗУ был {gb_text(peak)} — больше заявки {ram:g} ГБ: "
                           f"при нехватке памяти такую задачу заморозят первой")
        done = [r for r in past if r["state"] == "done" and (r["run_s"] or 0) > 0]
        if done:
            runs = sorted(r["run_s"] for r in done)
            med = runs[len(runs) // 2]
            if time_s < med * 0.8:
                out.append(f"в прошлых запусках она работала ~{texts.fmt_minutes(med)}, а заявлено ~{texts.fmt_minutes(time_s)}: "
                           f"может стать сверхурочной (поправить на ходу: rmq retime)")
            elif time_s > max(med * 3, med + 600):
                out.append(f"в прошлых запусках она работала ~{texts.fmt_minutes(med)}, а заявлено ~{texts.fmt_minutes(time_s)}: "
                           f"длинная заявка — меньше очков в очереди")
            here = [r for r in done if not r["remote_node"] and (r["run_s"] or 0) > 60 and r["cpu_s"]]
            if here:
                avg = sum(r["cpu_s"] / r["run_s"] for r in here) / len(here)
                if cpu > max(avg * 2, avg + 2):
                    out.append(f"в прошлых запусках она занимала в среднем ~{avg:.1f} ядра из заявленных {cpu:g}")
        return out

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ApiError(400, "режим: auto, game или paused")
        with self.lock:
            self.mode = mode
            self.db.kv_set("mode", mode)
            self.db.event("mode", {"auto": "Режим: авто", "game": "Режим: игровой (вручную)",
                                   "paused": "Режим: пауза всех задач"}[mode])
        self.wake.set()
        return {"mode": mode}

    def prepare_shutdown(self, tasks: str) -> dict:
        """freeze: tasks are suspended until the next start; keep: they run on; cancel: they are stopped."""
        if tasks not in ("freeze", "keep", "cancel"):
            raise ApiError(400, "tasks: freeze, keep или cancel")
        # stop scheduling first, or the next tick would resume what we freeze below
        self._stop.set()
        self.wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        n = 0
        with self.lock:
            now = time.time()
            for row in self.db.tasks_active():
                rt = self.runtimes.get(row["id"])
                if tasks == "cancel":
                    self._drop_runtime(row["id"], kill=True)
                    self._finalize(row, "cancelled", None, "менеджер выключен с отменой задач")
                    n += 1
                elif tasks == "freeze" and self._stopping(row):  # frozen, it could never finish saving
                    self._requeue_row(row, row["state_reason"][9:], now)
                    n += 1
                elif tasks == "freeze" and row["state"] == "running" and rt:
                    u = rt.usage
                    rt.suspend(trim=False)
                    self.db.task_update(row["id"], state="suspended", state_reason="shutdown", queued_since=now,
                                        resume_ram_gb=u.ws / GB, suspended_pids=json.dumps(sorted(rt.suspended_pids)),
                                        run_s=self.run_time(row, now))
                    self.db.event("suspend", "Заморожена: менеджер выключается", task_id=row["id"],
                                  agent_id=row["agent_id"])
                    n += 1
            self.db.event("shutdown", {"freeze": f"Менеджер выключен, задач заморожено: {n}",
                                       "keep": "Менеджер выключен, задачи продолжают работать",
                                       "cancel": f"Менеджер выключен, задач отменено: {n}"}[tasks])
        return {"ok": True, "tasks": tasks, "affected": n}

    def request_restart(self) -> None:
        """For an update: freeze the tasks, stop the server; the update helper starts the manager again."""
        self.prepare_shutdown("freeze")
        if self.restart_cb:
            threading.Timer(0.5, self.restart_cb).start()

    def update_settings(self, patch: dict) -> dict:
        try:
            data = self.settings.update(patch)
        except SettingsError as e:
            raise ApiError(400, str(e)) from e
        self.db.event("settings", "Настройки изменены: " + ", ".join(
            f"{sec}.{key}" for sec, vals in patch.items() if isinstance(vals, dict) for key in vals)[:300])
        self.wake.set()
        return self.settings_view(data)

    def reset_settings(self) -> dict:
        data = self.settings.reset()
        self.db.event("settings", "Настройки сброшены к значениям по умолчанию")
        self.wake.set()
        return self.settings_view(data)

    def settings_view(self, data: dict | None = None) -> dict:
        """The settings plus the presets and which of them they match (None: the owner's own values)."""
        from .settings import PRESETS, active_preset
        data = data if data is not None else self.settings.snapshot()
        data["_presets"] = {"list": [{"id": k, "title": t, "text": d} for k, (t, d, _o) in PRESETS.items()],
                            "active": active_preset(data)}
        return data

    def apply_preset(self, name: str) -> dict:
        from .settings import PRESETS
        try:
            data = self.settings.apply_preset(name)
        except SettingsError as e:
            raise ApiError(400, str(e)) from e
        self.db.event("settings", f"Режим настроек: {PRESETS[name][0]}")
        self.wake.set()
        return self.settings_view(data)

    # ---------------------------------------------------------------- agents
    def agent_json(self, a: dict, counts: dict | None = None) -> dict:
        fs = self.plan.fairshare.get(a["id"]) if self.plan else None
        c = (counts or {}).get(a["id"], {})
        return {"id": a["id"], "name": a["name"], "weight": a["weight"],
                "max_priority": PRIORITY_NAMES[a["max_priority"]], "max_running": a["max_running"],
                "max_queued": a["max_queued"], "usage": round(self.agent_usage.get(a["id"], a["usage"]), 1),
                "fairshare": fs, "enabled": bool(a["enabled"]), "builtin": bool(a["builtin"]), "note": a["note"],
                "running": c.get("running", 0), "suspended": c.get("suspended", 0), "queued": c.get("queued", 0)}

    def update_agent(self, agent_id: int, body: dict) -> dict:
        row = self.db.agent(agent_id)
        if not row:
            raise ApiError(404, "агент не найден")
        fields = {}
        try:
            if "weight" in body:
                fields["weight"] = max(0.01, float(body["weight"]))
            if "max_priority" in body:
                fields["max_priority"] = parse_priority(body["max_priority"])
            if "max_running" in body:
                fields["max_running"] = max(1, int(body["max_running"]))
            if "max_queued" in body:
                fields["max_queued"] = max(1, int(body["max_queued"]))
            if "enabled" in body:
                if row["builtin"] and not body["enabled"]:
                    raise ApiError(400, "встроенного агента отключить нельзя")
                fields["enabled"] = int(bool(body["enabled"]))
            if "note" in body:
                fields["note"] = str(body["note"])[:500]
            if body.get("reset_usage"):
                self.agent_usage[agent_id] = 0.0
                fields["usage"] = 0.0
        except (TypeError, ValueError) as e:
            raise ApiError(400, str(e)) from e
        self.db.agent_update(agent_id, **fields)
        self.wake.set()
        return self.agent_json(self.db.agent(agent_id))

    # ---------------------------------------------------------------- read models
    def task_json(self, row: dict, agents: dict[int, dict], full: bool = False) -> dict:
        tid = row["id"]
        plan = self.plan
        lv = self.live.get(tid) or {}
        u = self.state.get("_usage", {}).get(tid)
        wait = plan.waiting.get(tid) if plan and row["state"] in ("queued", "suspended") else None
        run_s = self.run_time(row, time.time())
        held_s = lv.get("held_s") or row.get("held_s") or 0.0
        d = {
            "id": tid, "agent": agents.get(row["agent_id"], {}).get("name", "?"), "agent_id": row["agent_id"],
            "name": row["name"], "command": command_text(row), "cwd": row["cwd"], "state": row["state"],
            "state_text": "зависла" if row["state"] == "suspended" and row["state_reason"] == "hung"
                          else texts.STATE_NAMES.get(row["state"], row["state"]),
            "reason": row["state_reason"], "reason_text": texts.describe(row["state_reason"]),
            "cpu": row["cpu"], "ram": row["ram_gb"], "vram": row["vram_gb"], "time_s": row["time_s"],
            "time_limit_s": row["time_limit_s"], "priority": PRIORITY_NAMES.get(row["priority"], "normal"),
            "preempt": row["preempt"], "submitted_at": row["submitted_at"], "queued_since": row["queued_since"],
            "started_at": row["started_at"], "last_start_at": row["last_start_at"], "finished_at": row["finished_at"],
            "run_s": run_s, "restarts": row["restarts"], "exit_code": row["exit_code"],
            "peak_ram": max(row["peak_ram_gb"], lv.get("peak_ram_gb", 0.0)),
            "peak_vram": max(row["peak_vram_gb"], lv.get("peak_vram_gb", 0.0)),
            "held_s": held_s,
            "overtime": row["state"] in ("running", "suspended") and run_s - held_s > row["time_s"] * (
                1 + self.settings.section("scheduler")["overtime_grace_pct"] / 100),
            "series": row.get("series"), "after_id": row.get("after_id"),
            "swing": self._swings.get(tid) if row["state"] == "running" else None,
            "cpu_s": row["cpu_s"],
            "conditions": self.conditions(row) if row["started_at"] and not row["remote_node"] else None,
            "score": plan.scores.get(tid) if plan else None,
            "standing": plan.standing.get(tid) if plan and row["state"] == "running" else None,
            "position": wait.position if wait and wait.position else None,
            "wait": wait.reason if wait else None,
            "wait_text": texts.describe(wait.reason, wait.detail) if wait else "",
            "cap_cores": plan.cpu_caps.get(tid) if plan and row["state"] == "running" else None,
            "progress": self._progress(tid, run_s) if row["state"] in ("running", "suspended") else None,
            "used": None,
        }
        if wait and wait.reason == "no_fit" and "cpu" in (wait.detail or {}).get("lacking", []) \
                and ((self.state.get("system") or {}).get("cpu") or {}).get("on_ecores"):
            d["wait_text"] += (". Идёт игра владельца: задачам доступны только E-ядра, а их уже занимают работающие "
                               "задачи — новая стартует, когда какая-то закончится или игра кончится")
        if row["state"] == "remote" or row["remote_node"]:
            rs = json.loads(row["remote_status"] or "{}")
            remote = rs.get("remote") or {}
            where = self.net.node_name(row["remote_node"])
            d["where"] = where
            d["remote"] = {k: remote.get(k) for k in ("state", "state_text", "reason_text", "wait_text", "position",
                                                       "run_s", "progress", "used", "exit_code", "cap_cores", "hung",
                                                       "started_at", "swing", "conditions")}
            if remote.get("started_at"):  # when it really started there, not when it was sent
                d["started_at"] = remote["started_at"]
            if row["state"] == "remote":
                inner = remote.get("state_text") or "отправляется"
                phase = rs.get("phase_text") or ""
                d["state_text"] = f"на {where}: {inner}"
                d["reason_text"] = "; ".join(x for x in (phase, remote.get("reason_text") or remote.get("wait_text") or "")
                                             if x)
                d["run_s"] = remote.get("run_s") or 0.0
                d["progress"] = remote.get("progress")
                d["used"] = remote.get("used")
        if row["remote_node"]:
            d["where_node"] = row["remote_node"]
        if row["origin_node"]:
            d["from"] = self.net.node_name(row["origin_node"])
            d["from_node"] = row["origin_node"]
        if self._stopping(row):
            grace = self.settings.section("tasks")["stop_grace_s"]
            d["stopping"] = {"reason": texts.describe(row["state_reason"][9:]),
                             "left_s": max(0.0, (row["stop_requested_at"] or 0.0) + grace - time.time())}
        act = self._activity.get(tid)
        if row["state"] == "running" and act:
            d["idle_s"] = max(0.0, time.time() - act["at"])
            d["hung"] = act["hung"]
        if u is not None:
            usage, vram, gutil = u
            d["used"] = {"cpu": usage.cpu, "ram": usage.ws / GB, "vram": vram, "gpu": gutil,
                         "procs": max(0, len(usage.pids))}
        if full:
            d["log_size"] = self._log_size(tid)
        return d

    def _progress(self, tid: int, run_s: float) -> dict | None:
        """What the task reports in RMQ_PROGRESS_FILE, or else the last tqdm bar in its log; plus an ETA."""
        tdir = self.task_dir(tid)
        pfile, log = tdir / "progress.txt", tdir / "output.log"
        try:
            pstamp = pfile.stat().st_mtime_ns
        except OSError:
            pstamp = None
        try:
            lsize = log.stat().st_size
        except OSError:
            lsize = None
        key = (pstamp, lsize)
        cached = self._progress_cache.get(tid)
        if cached and cached[0] == key:
            res = cached[1]
        else:
            res = None
            if pstamp is not None:
                try:
                    res = progress.parse_explicit(pfile.read_text(encoding="utf-8", errors="replace")[:4096])
                except OSError:
                    res = None
                if res:
                    res["source"] = "задача"
            if res is None and lsize:
                try:
                    with open(log, "rb") as f:
                        f.seek(max(0, lsize - 16384))
                        res = progress.parse_tqdm(f.read().decode("utf-8", errors="replace"))
                except OSError:
                    res = None
                if res:
                    res["source"] = "tqdm в логе"
            if res is None and cached:   # keep the last known value while the task prints something else
                res = cached[1]
            self._progress_cache[tid] = (key, res)
        if res and res["pct"] >= 1 and run_s >= 30:
            res = dict(res, eta_s=run_s * (100.0 - res["pct"]) / res["pct"])
        if res:
            res = dict(res, **progress_pace(self._phist.setdefault(tid, deque(maxlen=500)), run_s, res))
        return res

    def _log_size(self, tid: int) -> int:
        try:
            return (self.task_dir(tid) / "output.log").stat().st_size
        except OSError:
            return 0

    def get_state(self, ident: Identity) -> dict:
        with self.lock:
            state = {k: v for k, v in self.state.items() if not k.startswith("_")}
            agents = {a["id"]: a for a in self.db.agents()}
            active = self.db.tasks_active() + self.db.tasks_remote()
            counts: dict[int, dict] = {}
            for r in active:
                c = counts.setdefault(r["agent_id"], {})
                c[r["state"]] = c.get(r["state"], 0) + 1
            tasks = [self.task_json(r, agents) for r in active]
            tasks.sort(key=lambda t: ({"running": 0, "remote": 1, "suspended": 2, "queued": 3}[t["state"]],
                                      t["position"] or 0, t["id"]))
            state["tasks"] = tasks
            state["recent"] = [self.task_json(r, agents) for r in self.db.tasks_recent(30)]
            state["agents"] = [self.agent_json(a, counts) for a in agents.values()]
            state["me"] = {"admin": ident.admin, "agent": ident.agent["name"]}
            state["events"] = self.db.events(limit=60)
        state["home_running"] = self.net.home_running()  # outside the lock: it takes the network's own
        return state

    def running_brief(self) -> list[dict]:
        """The tasks running on this PC right now, in short: one's other PCs show them on their main page."""
        with self.lock:
            agents = {a["id"]: a for a in self.db.agents()}
            out = []
            for r in self.db.tasks_active():
                if r["state"] != "running":
                    continue
                t = self.task_json(r, agents)
                brief = {k: t.get(k) for k in ("id", "name", "command", "agent", "from", "cpu", "ram", "vram", "used",
                                               "run_s", "time_s", "progress", "priority", "overtime")}
                brief["origin_node"] = r["origin_node"]
                out.append(brief)
            return out

    def home_state(self) -> dict:
        """This PC's dashboard state as one of the owner's other PCs sees it (read-only, without the journal)."""
        state = self.get_state(Identity(True, self.db.agent(self.user_agent_id)))
        for key in ("events", "agents", "home_running"):
            state.pop(key, None)
        return state

    def metrics_view(self, rng: float, since: float) -> dict:
        """The chart series of the last `rng` seconds (newer than `since`) and the names of the tasks in them."""
        res = self.metrics.series(max(10.0, min(30 * 86400.0, rng)), since)
        ids = {int(k) for row in res.get("tasks", []) for k in (row or {})}
        res["task_names"] = self.db.task_labels(ids)
        return res

    def get_task(self, ident: Identity, tid: int) -> dict:
        with self.lock:
            row = self._task_for(ident, tid)
            agents = {a["id"]: a for a in self.db.agents()}
            d = self.task_json(row, agents, full=True)
            d["events"] = self.db.events(task_id=tid, limit=50)
            return d

    def read_log(self, ident: Identity, tid: int, offset: int, limit: int) -> dict:
        row = self._task_for(ident, tid)
        if row["state"] == "remote":
            return self._remote_log(row, offset, limit)
        path = self.task_dir(tid) / "output.log"
        limit = max(1, min(limit, 1 << 20))
        try:
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if offset < 0:
                    offset = max(0, size + offset)
                rotated = offset > size
                if rotated:
                    offset = 0
                f.seek(offset)
                data = f.read(limit)
        except FileNotFoundError:
            return {"data": "", "offset": 0, "size": 0, "rotated": False}
        text = None
        for cut in range(4):  # a multi-byte char cut at the end is read next time
            chunk = data[:len(data) - cut]
            try:
                text, data = chunk.decode("utf-8"), chunk
                break
            except UnicodeDecodeError:
                continue
        if text is None:  # not UTF-8 at all (some console programs write cp1251)
            text = data.decode("cp1251", errors="replace")
        return {"data": text, "offset": offset + len(data), "size": size, "rotated": rotated}
