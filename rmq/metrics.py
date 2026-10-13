"""In-memory history for the charts: 1 s points for the last hour, 15 s averages for a day.

Besides the fixed columns every point carries what each running task used then:
{task id: [CPU % of the whole processor, RAM GB, VRAM GB, GPU %]}.

The history outlives restarts and updates: the manager saves it (gzip JSON, a few hundred KB) on stop and every
few minutes, and loads it on start. Without that file (after a crash) the day is rebuilt, coarser, from the
per-minute statistics in data/stats."""
from __future__ import annotations

import gzip
import json
import os
import threading
from collections import deque
from pathlib import Path

CACHE_VERSION = 1

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
        self.fine_s, self.coarse_s = fine_s, coarse_s
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

    # ------------------------------------------------------------ across restarts
    def save(self, path: Path) -> None:
        with self._lock:
            data = {"v": CACHE_VERSION, "columns": list(COLUMNS), "step": self.coarse_step,
                    "fine": list(self._fine), "coarse": list(self._coarse)}
        tmp = path.with_name(path.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)

    def load(self, path: Path, now: float) -> float:
        """The saved history (columns matched by name, so it survives new columns); the time of its last point."""
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, EOFError):
            return 0.0
        if data.get("v") != CACHE_VERSION or data.get("step") != self.coarse_step:
            return 0.0
        cols = list(data.get("columns") or [])
        where = [cols.index(c) + 1 if c in cols else None for c in COLUMNS]

        def row(r: list) -> tuple:
            per = r[len(cols) + 1] if len(r) > len(cols) + 1 and isinstance(r[len(cols) + 1], dict) else {}
            return (r[0],) + tuple(r[i] if i is not None and i < len(r) else None for i in where) + (per,)

        fine = [row(r) for r in data.get("fine") or [] if r and r[0] > now - self.fine_s]
        coarse = [row(r) for r in data.get("coarse") or [] if r and r[0] > now - self.coarse_s]
        with self._lock:
            self._fine.extend(fine)
            self._coarse.extend(coarse)
            last = max([r[0] for r in fine[-1:] + coarse[-1:]], default=0.0)
            self._last_t = max(self._last_t, last)
        return last

    def seed(self, minutes: list[dict], now: float) -> int:
        """Rebuild history from per-minute rows ({"t": minute start, column: value}): each minute is repeated
        across it, so the charts draw it as a line, not as separate dots."""
        fine, coarse = [], []
        for m in sorted(minutes, key=lambda x: x["t"]):
            vals = tuple(_num(m.get(c)) for c in COLUMNS)
            for k in range(0, 60, self.coarse_step):  # never past now: the last minute may be unfinished
                if now - self.coarse_s < m["t"] + k <= now - 1:
                    coarse.append((round(m["t"] + k, 2),) + vals + ({},))
            for k in range(0, 60, 2):
                if now - self.fine_s < m["t"] + k <= now - 1:
                    fine.append((round(m["t"] + k, 2),) + vals + ({},))
        with self._lock:
            self._fine.extend(fine)
            self._coarse.extend(coarse)
            if fine or coarse:
                self._last_t = max(self._last_t, (fine or coarse)[-1][0])
        return len(minutes)

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
