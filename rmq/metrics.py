"""In-memory history for the charts: 1 s points for the last hour, 15 s averages for a day.

Besides the fixed columns every point carries what each running task used then:
{task id: [CPU % of the whole processor, RAM GB, VRAM GB, GPU %]}."""
from __future__ import annotations

import threading
from collections import deque

COLUMNS = (
    "cpu_user", "cpu_tasks", "cpu_limit",          # % of the whole CPU
    "ram_user", "ram_tasks", "ram_limit",          # GB
    "vram_user", "vram_tasks", "vram_limit",       # GB
    "gpu_user", "gpu_tasks",                        # %
    "running", "queued", "suspended", "game",
    "temp_cpu", "temp_board", "temp_gpu", "temp_ssd",  # degrees C, None = not measured
    "heat",                                        # 0 normal, 1 the regulator cuts the CPU, 2 hot (frozen)
    "cpu_factor",                                  # % of their CPU the temperature regulator leaves tasks
)


def _num(v):
    return None if v is None else round(float(v), 3)


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


TASK_FIELDS = 4


def _tasks(per: dict | None) -> dict:
    return {str(k): [round(float(x), 2) for x in v[:TASK_FIELDS]] for k, v in (per or {}).items()}


def _avg_tasks(rows: list[dict]) -> dict:
    """A task missing from a point used nothing then."""
    keys = {k for r in rows for k in r}
    n = max(1, len(rows))
    return {k: [round(sum((r.get(k) or [0.0] * TASK_FIELDS)[i] for r in rows) / n, 2) for i in range(TASK_FIELDS)]
            for k in keys}


class Metrics:
    def __init__(self, fine_s: int = 3600, coarse_step: int = 15, coarse_s: int = 86400):
        self._lock = threading.Lock()
        self.coarse_step = coarse_step
        self._fine: deque[tuple] = deque(maxlen=fine_s)
        self._coarse: deque[tuple] = deque(maxlen=coarse_s // coarse_step)
        self._acc: list[tuple] = []
        self._last_t = 0.0

    def add(self, t: float, values: dict) -> None:
        row = (round(t, 2),) + tuple(_num(values.get(c, 0.0)) for c in COLUMNS) + (_tasks(values.get("tasks")),)
        with self._lock:
            if t - self._last_t < 0.95:
                return
            self._last_t = t
            self._fine.append(row)
            self._acc.append(row)
            if t - self._acc[0][0] >= self.coarse_step:
                avg = tuple(_avg(r[i] for r in self._acc) for i in range(len(row) - 1)) + (
                    _avg_tasks([r[-1] for r in self._acc]),)
                self._coarse.append(avg)
                self._acc = []

    def series(self, range_s: float, since: float = 0.0) -> dict:
        with self._lock:
            src = self._fine if range_s <= 3600 else self._coarse
            step = 1 if range_s <= 3600 else self.coarse_step
            last = src[-1][0] if src else 0.0
            lo = max(since, last - range_s)
            rows = [r for r in src if r[0] > lo]
        out = {"step": step, "t": [r[0] for r in rows]}
        for i, c in enumerate(COLUMNS, 1):
            out[c] = [r[i] for r in rows]
        out["tasks"] = [r[-1] for r in rows]
        return out
