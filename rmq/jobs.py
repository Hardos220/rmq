"""Control over one task's process tree through a named Windows job object.

Only processes the manager launched itself are ever touched. A task runs as
    job object  ->  wrapper.py (writes the log and the exit code)  ->  the command
The wrapper keeps a handle to the job so that the job (and its name) outlives a
manager restart; the manager re-attaches by name. Tasks do not die with the manager.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import psutil

from . import winapi as w

GB = 1024 ** 3


@dataclass
class Usage:
    cpu: float = 0.0                 # cores
    ws: int = 0                      # working set (physical RAM), bytes
    private: int = 0                 # private commit, bytes
    active: int = 0                  # live processes in the job
    pids: list[int] = field(default_factory=list)
    cpu_s: float = 0.0               # CPU seconds the job has used since it was created
    io_read: int = 0                 # bytes the job has read and written since it was created
    io_write: int = 0


class TaskRuntime:
    def __init__(self, task_id: int, job, job_name: str):
        self.task_id = task_id
        self.job = job
        self.job_name = job_name
        self.suspended_pids: set[int] = set()
        self.usage = Usage()
        self.popen: subprocess.Popen | None = None
        self.finished_at: float | None = None   # when the wrapper reported the exit (orphans get a grace)
        self.low_io = False
        self._cpu_last: tuple[float, float] | None = None
        self._configured: set[int] = set()
        self._procs: dict[int, psutil.Process] = {}
        self._rate: int | None = None
        self._limits: tuple | None = None
        self._eco: bool | None = None

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def launch(cls, task_id: int, job_name: str, cmd: list[str], *, cwd: str, env: dict[str, str],
               affinity: int, job_memory: int | None, low_io: bool, low_integrity: bool = False) -> "TaskRuntime":
        """`low_integrity`: a task from another PC of the network, which must not change this PC's files."""
        job = w.create_job(job_name)
        rt = cls(task_id, job, job_name)
        rt.low_io = low_io
        try:
            rt.set_limits(affinity, job_memory)
            flags = w.CREATE_SUSPENDED | w.CREATE_NO_WINDOW | w.CREATE_NEW_PROCESS_GROUP | w.IDLE_PRIORITY_CLASS
            kw = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, close_fds=True)
            if low_integrity:
                rt.popen = w.create_low_integrity_process(cmd, cwd=cwd, env=env,
                                                          flags=flags | w.CREATE_BREAKAWAY_FROM_JOB)
            else:
                try:  # leave the manager's own job (if any) so tasks are independent of it
                    rt.popen = subprocess.Popen(cmd, creationflags=flags | w.CREATE_BREAKAWAY_FROM_JOB, **kw)
                except PermissionError:
                    rt.popen = subprocess.Popen(cmd, creationflags=flags, **kw)
            h = w.open_process(rt.popen.pid)
            if not h:
                rt.popen.kill()
                raise OSError("cannot open the task process")
            try:
                w.AssignProcessToJobObject(job, h)
                w.set_low_memory_priority(h)
                if low_io:
                    w.set_low_io_priority(h)
                w.resume_process(h)
            finally:
                w.CloseHandle(h)
            rt._configured.add(rt.popen.pid)
        except BaseException:
            try:
                w.terminate_job(job, 1)
            except OSError:
                pass
            w.CloseHandle(job)
            raise
        return rt

    @classmethod
    def adopt(cls, task_id: int, job_name: str) -> "TaskRuntime | None":
        job = w.open_job(job_name)
        return cls(task_id, job, job_name) if job else None

    def close(self) -> None:
        """Release our handle only; the task keeps running (the wrapper holds the job too)."""
        if self.job:
            w.CloseHandle(self.job)
            self.job = None
        if hasattr(self.popen, "close"):
            self.popen.close()

    # ------------------------------------------------------------ observation
    def pids(self) -> list[int]:
        return w.job_pids(self.job)

    def sample(self, now: float) -> Usage:
        cpu_s, active, io_read, io_write = w.job_accounting_io(self.job)
        u = Usage(active=active, cpu_s=cpu_s, io_read=io_read, io_write=io_write)
        if self._cpu_last is not None:
            dt = now - self._cpu_last[1]
            if dt > 0:
                u.cpu = max(0.0, (cpu_s - self._cpu_last[0]) / dt)
        self._cpu_last = (cpu_s, now)
        u.pids = self.pids()
        for pid in u.pids:
            proc = self._procs.get(pid)
            try:
                if proc is None:
                    proc = self._procs[pid] = psutil.Process(pid)
                mi = proc.memory_info()
                u.ws += mi.rss
                u.private += getattr(mi, "private", mi.vms)
            except psutil.Error:
                self._procs.pop(pid, None)
        for pid in list(self._procs):
            if pid not in u.pids:
                del self._procs[pid]
        self._configure_new(u.pids)
        self.usage = u
        return u

    def cpu_seconds(self) -> float:
        return w.job_accounting(self.job)[0]

    def peak_commit(self) -> int:
        return w.job_peak_memory(self.job)

    def _configure_new(self, pids: list[int]) -> None:
        """Children spawned by the task get the same treatment as the task itself."""
        for pid in pids:
            if pid in self._configured:
                continue
            self._configured.add(pid)
            h = w.open_process(pid)
            if not h:
                continue
            try:
                w.set_low_memory_priority(h)
                if self.low_io:
                    w.set_low_io_priority(h)
                if self._eco is not None:
                    w.set_eco_qos(h, self._eco)
                if self.suspended_pids and pid not in self.suspended_pids:
                    if w.suspend_process(h):
                        self.suspended_pids.add(pid)
            finally:
                w.CloseHandle(h)
        self._configured &= set(pids)

    # ------------------------------------------------------------ control
    def set_limits(self, affinity: int, job_memory: int | None) -> None:
        key = (affinity, job_memory)
        if key != self._limits:
            w.job_set_limits(self.job, priority_class=w.IDLE_PRIORITY_CLASS, affinity=affinity,
                             job_memory=job_memory)
            self._limits = key

    def set_cpu_cap(self, cores: float, n_cpu: int) -> None:
        rate = max(1, min(10000, int(round(cores / n_cpu * 10000))))
        if self._rate is None or abs(rate - self._rate) > max(20, self._rate * 0.02):
            w.job_set_cpu_rate(self.job, rate)
            self._rate = rate

    def set_eco(self, enabled: bool) -> None:
        if enabled == self._eco:
            return
        self._eco = enabled
        for pid in self.pids():
            h = w.open_process(pid)
            if h:
                try:
                    w.set_eco_qos(h, enabled)
                finally:
                    w.CloseHandle(h)

    def suspend(self, trim: bool = True) -> None:
        """Freeze every process of the task and push its memory out of RAM."""
        for _ in range(3):  # catch processes spawned while we were suspending
            new = [p for p in self.pids() if p not in self.suspended_pids]
            if not new:
                break
            for pid in new:
                h = w.open_process(pid)
                if not h:
                    continue
                try:
                    if w.suspend_process(h):
                        self.suspended_pids.add(pid)
                    if trim:
                        w.empty_working_set(h)
                finally:
                    w.CloseHandle(h)

    def trim(self) -> None:
        """Push a frozen task's memory out of RAM (only when the owner actually needs the memory)."""
        for pid in self.pids():
            h = w.open_process(pid)
            if h:
                try:
                    w.empty_working_set(h)
                finally:
                    w.CloseHandle(h)

    def resume(self) -> None:
        live = set(self.pids())
        for pid in list(self.suspended_pids):
            if pid in live:
                h = w.open_process(pid)
                if h:
                    try:
                        w.resume_process(h)
                    finally:
                        w.CloseHandle(h)
        self.suspended_pids.clear()

    def kill(self) -> None:
        try:
            w.terminate_job(self.job, 1)
        except OSError:
            pass
        self.suspended_pids.clear()
