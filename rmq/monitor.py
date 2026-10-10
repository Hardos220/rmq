"""Read-only sampling of the machine: CPU, RAM, commit charge and the GPU (via PDH counters)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import winapi

NVIDIA, AMD, INTEL = 0x10DE, 0x1002, 0x8086
_PROC_RE = re.compile(r"^pid_(\d+)_(luid_0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+$")
_ENG_RE = re.compile(r"^pid_(\d+)_(luid_0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_engtype_(.*)$")
_ADAPTER_RE = re.compile(r"^(luid_0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+$")
# Engines whose load says nothing about games/compute (video playback, overlays...).
_IGNORED_ENGINES = ("video", "security", "overlay", "jpeg", "vr")


@dataclass
class GpuSample:
    used: int = 0                                   # dedicated VRAM in use, bytes
    proc_vram: dict[int, int] = field(default_factory=dict)
    engines: dict[str, tuple[str, dict[int, float]]] = field(default_factory=dict)  # eng -> (type, pid -> %)

    def util(self, pids: set[int] | None = None) -> float:
        """Task-Manager-style load: the busiest engine, optionally counting only `pids`."""
        best = 0.0
        for _typ, per_pid in self.engines.values():
            s = sum(v for p, v in per_pid.items() if pids is None or p in pids)
            best = max(best, s)
        return min(100.0, best)

    def user_util(self, managed: set[int]) -> float:
        """Load from everything except managed tasks, on 3D/compute/copy engines only."""
        best = 0.0
        for typ, per_pid in self.engines.values():
            if any(k in typ for k in _IGNORED_ENGINES):
                continue
            best = max(best, sum(v for p, v in per_pid.items() if p not in managed))
        return min(100.0, best)

    def vram_of(self, pids) -> int:
        return sum(self.proc_vram.get(p, 0) for p in pids)


@dataclass
class SystemSample:
    t: float
    cpu_frac: float          # 0..1 of the whole machine
    ram_total: int
    ram_available: int
    commit_total: int
    commit_limit: int
    gpu: GpuSample | None


def pick_adapter(adapters: list[dict], wanted: str) -> dict | None:
    if not adapters:
        return None
    if wanted and wanted != "auto":
        for a in adapters:
            if wanted.lower() in a["name"].lower():
                return a
        return None
    discrete = [a for a in adapters if a["vendor"] in (NVIDIA, AMD) or a["vram"] >= 1 << 30]
    return max(discrete or adapters, key=lambda a: a["vram"])


class GpuMonitor:
    def __init__(self, adapter: dict):
        self.adapter = adapter
        self.luid = adapter["luid"].lower()
        self.total = adapter["vram"]
        self._q = winapi.PdhQuery()
        ok = (self._q.add("amem", r"\GPU Adapter Memory(*)\Dedicated Usage", winapi.PDH_FMT_LARGE)
              & self._q.add("pmem", r"\GPU Process Memory(*)\Dedicated Usage", winapi.PDH_FMT_LARGE))
        self.has_engines = self._q.add("eng", r"\GPU Engine(*)\Utilization Percentage",
                                       winapi.PDH_FMT_DOUBLE | winapi.PDH_FMT_NOCAP100)
        if not ok:
            raise OSError("GPU performance counters are not available")
        self._q.collect()

    def sample(self) -> GpuSample:
        self._q.collect()
        s = GpuSample()
        for name, v in self._q.values("amem").items():
            m = _ADAPTER_RE.match(name.lower())
            if m and m.group(1) == self.luid:
                s.used += int(v)
        for name, v in self._q.values("pmem").items():
            m = _PROC_RE.match(name.lower())
            if m and m.group(2) == self.luid and v > 0:
                pid = int(m.group(1))
                s.proc_vram[pid] = s.proc_vram.get(pid, 0) + int(v)
        if self.has_engines:
            for name, v in self._q.values("eng").items():
                m = _ENG_RE.match(name.lower())
                if not m or m.group(2) != self.luid or v <= 0:
                    continue
                key = f"{m.group(3)}_{m.group(4)}"
                typ, per_pid = s.engines.setdefault(key, (m.group(5), {}))
                pid = int(m.group(1))
                per_pid[pid] = per_pid.get(pid, 0.0) + v
        return s


class SystemSampler:
    def __init__(self, gpu_adapter: str = "auto"):
        self._last = winapi.system_times()
        self.adapters = winapi.dxgi_adapters()
        self.gpu: GpuMonitor | None = None
        self.gpu_error = ""
        adapter = pick_adapter(self.adapters, gpu_adapter)
        if adapter:
            try:
                self.gpu = GpuMonitor(adapter)
            except OSError as e:
                self.gpu_error = str(e)

    def sample(self) -> SystemSample:
        idle, kern, user = winapi.system_times()
        li, lk, lu = self._last
        self._last = (idle, kern, user)
        total = (kern - lk) + (user - lu)
        busy = total - (idle - li)
        cpu = min(1.0, max(0.0, busy / total)) if total > 0 else 0.0
        pi = winapi.performance_info()
        gpu = None
        if self.gpu is not None:
            try:
                gpu = self.gpu.sample()
            except OSError:
                gpu = None
        return SystemSample(time.time(), cpu, pi["ram_total"], pi["ram_available"],
                            pi["commit_total"], pi["commit_limit"], gpu)
