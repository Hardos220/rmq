"""Whether a task's CPU use goes up and down in waves, and how fast (standard library only: the CLI uses it too).

A steady computation keeps its cores busy. Waves - busy, then almost idle, again and again - usually mean phases:
a check after each batch, a wait for a lock, files or the network, a pool that runs dry. Worth knowing: the task
takes longer than its cores could do, and the cooling heats and cools with it.
"""
from __future__ import annotations

import math


def swing(times: list[float], cores: list[float | None], window_s: float = 600.0) -> dict | None:
    """{"lo", "hi" (cores, the 10th and 90th percentile), "period_s" (None when not regular)} of the last
    `window_s` seconds, or None when the load is steady or there is too little to tell."""
    if not times:
        return None
    end = times[-1]
    pts = [(t, v) for t, v in zip(times, cores) if v is not None and t >= end - window_s]
    if len(pts) < 30 or pts[-1][0] - pts[0][0] < 180:
        return None
    vals = sorted(v for _, v in pts)
    lo, hi = vals[len(vals) // 10], vals[(len(vals) * 9) // 10]
    if hi - lo < max(0.5, 0.4 * hi):
        return None
    return {"lo": round(lo, 2), "hi": round(hi, 2), "period_s": _period(pts)}


def _period(pts: list[tuple[float, float]], step: float = 2.0) -> float | None:
    """The lag (s) at which the series best repeats itself, by autocorrelation on an even grid."""
    t0, t1 = pts[0][0], pts[-1][0]
    n = int((t1 - t0) // step) + 1
    grid, j = [], 0
    for k in range(n):  # the last sample at or before each grid point
        t = t0 + k * step
        while j + 1 < len(pts) and pts[j + 1][0] <= t:
            j += 1
        grid.append(pts[j][1])
    mean = sum(grid) / n
    x = [v - mean for v in grid]
    var = sum(v * v for v in x)
    if var <= 1e-9:
        return None
    prev, dipped = None, False
    for lag in range(3, min(n // 2, int(300 / step)) + 1):  # 6 s .. 5 min, and at least two cycles in the window
        r = sum(x[i] * x[i + lag] for i in range(n - lag)) / var
        dipped = dipped or r < 0.1                  # past the first trough: what rises now is the next cycle
        if dipped and prev is not None and r < prev and prev > 0.3:
            return round((lag - 1) * step)          # the first clear peak: the period, not a multiple of it
        prev = r
    return None


def describe(s: dict | None) -> str:
    """"колеблется 0,9–8,5 яд., период ~1,5 мин" for status lines."""
    if not s:
        return ""
    fmt = lambda v: f"{v:.1f}".replace(".", ",")
    p = s.get("period_s")
    per = "" if not p else f", период ~{p:.0f} с" if p < 90 else f", период ~{p / 60:.1f} мин".replace(".", ",")
    return f"колеблется {fmt(s['lo'])}–{fmt(s['hi'])} яд.{per}"


def from_metrics(m: dict, tid: int, n_cpu: int) -> dict | None:
    """The swing of one task from the dashboard's metrics (per point: {task: [CPU % of the PC, ...]})."""
    key = str(tid)
    cores = []
    for row in m.get("tasks") or []:
        v = (row or {}).get(key)
        cores.append(v[0] * n_cpu / 100.0 if v else 0.0)
    return swing(m.get("t") or [], cores)


def _selftest() -> None:  # pragma: no cover - a quick check by hand
    ts = [float(i) for i in range(600)]
    wave = [8.0 if (i // 45) % 2 == 0 else 0.5 for i in range(600)]
    print(swing(ts, wave), swing(ts, [4.0 + 0.1 * math.sin(i) for i in range(600)]))
