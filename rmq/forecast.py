"""When the tasks will likely start and end: the queue played forward over the run times expected of them.

Approximate by design. It assumes the owner keeps using the PC as now (the pool stays as it is) and that each task
runs as long as its earlier runs of the same command did (else as long as it declared, or as its progress says),
and it follows the scheduler's main rules: the score order with later tasks filling gaps, memory that must fit, the
CPU that may be squeezed by a quarter, the agents' limits on running tasks, series and --after. Games, pauses,
the heat and other agents' new tasks change it - so the dashboard shows it as "~15:40", not as a promise.

How much slower another of one's PCs runs the same commands (speed_ratio) comes from the same earlier runs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

CPU_SQUEEZE = 1.0 / (1.0 - 0.25)
HORIZON_S = 14 * 86400      # beyond two weeks a forecast says nothing useful


@dataclass
class Job:
    id: int
    agent: int
    cpu: float
    ram: float
    vram: float
    dur: float                  # seconds: all of it for a queued task, what is left for a running one
    running: bool = False
    series: str | None = None
    after: int | None = None


def simulate(now: float, pool_cpu: float, pool_ram: float, pool_vram: float, jobs: list[Job],
             max_running: dict[int, int], squeeze: float = CPU_SQUEEZE) -> dict[int, tuple[float, float]]:
    """{id: (start, end)} for every task it can place; queued ones are taken in the order given."""
    out: dict[int, tuple[float, float]] = {}
    active: list[tuple[float, Job]] = []
    for j in jobs:
        if j.running:
            out[j.id] = (now, now + j.dur)
            active.append((now + j.dur, j))
    waiting = [j for j in jobs if not j.running]
    in_sim = {j.id for j in jobs}
    ended: set[int] = set()
    t = now

    def fits(j: Job) -> bool:
        used_cpu = sum(a.cpu for _, a in active)
        used_ram = sum(a.ram for _, a in active)
        used_vram = sum(a.vram for _, a in active)
        if used_cpu + min(j.cpu, pool_cpu) > pool_cpu * squeeze + 1e-9:
            return False
        if used_ram + j.ram > pool_ram + 1e-6 or used_vram + j.vram > pool_vram + 1e-6:
            return False
        if sum(1 for _, a in active if a.agent == j.agent) >= max_running.get(j.agent, 4):
            return False
        if j.series:
            if any(a.series == j.series and a.agent == j.agent for _, a in active):
                return False
            if min(w.id for w in waiting if w.series == j.series and w.agent == j.agent) != j.id:
                return False   # the series goes in the order it was submitted
        if j.after is not None and j.after in in_sim and j.after not in ended:
            return False
        return True

    while waiting and t - now <= HORIZON_S:
        started = True
        while started:
            started = False
            for j in waiting:
                if fits(j):
                    out[j.id] = (t, t + j.dur)
                    active.append((t + j.dur, j))
                    waiting.remove(j)
                    started = True
                    break
        if not active:
            break   # nothing runs and nothing fits: the rest cannot start under these assumptions
        t = min(e for e, _ in active)
        for item in [a for a in active if a[0] <= t]:
            active.remove(item)
            ended.add(item[1].id)
    return out


def median(vals: list[float]) -> float | None:
    vals = sorted(v for v in vals if v and v > 0)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def speed_ratio(here: dict[str, list[float]], there: dict[str, list[float]]) -> tuple[float, int] | None:
    """How many times longer the same commands run there than here ({command key: run times}), weighed by how
    many runs each command has on both sides; (ratio, runs compared) or None without a command run on both."""
    num = den = 0.0
    n = 0
    for key, runs_there in there.items():
        a, b = median(here.get(key) or []), median(runs_there)
        if a is None or b is None:
            continue
        w = min(len(here[key]), len(runs_there))
        num += w * math.log(b / a)
        den += w
        n += len(runs_there)
    if den <= 0:
        return None
    return math.exp(num / den), n
