"""The life of each task on a timeline, rebuilt from the event log: waiting, running and frozen spans,
plus marks for restarts, stop requests, hangs and the outcome. Feeds the dashboard's "Хроника" tab."""
from __future__ import annotations

TERMINAL = {"done", "failed", "cancelled", "timeout", "lost"}
_PREFIXES = ("Пауза: ", "Перезапуск позже: ")


def _note(message: str) -> str:
    for p in _PREFIXES:
        if message.startswith(p):
            return message[len(p):]
    return message


def segments(task: dict, events: list[dict], now: float) -> tuple[list, list]:
    """([[start, end, "wait" | "run" | "frozen", note], ...], [[time, kind, message], ...]) of one task."""
    segs: list[list] = []
    marks: list[list] = []
    state: str | None = None
    since = 0.0
    note = ""

    def close(t: float) -> None:
        if state is None or t <= since:
            return
        last = segs[-1] if segs else None
        if last and last[2] == state and last[3] == note and last[1] == round(since, 1):
            last[1] = round(t, 1)  # e.g. a resume after a manager restart: still the same span
        else:
            segs.append([round(since, 1), round(t, 1), state, note])

    for e in sorted(events, key=lambda e: (e["ts"], e["id"])):
        kind, t, msg = e["kind"], e["ts"], e["message"]
        if kind == "submit":
            close(t)
            state, since, note = "wait", t, ""
        elif kind in ("start", "resume"):
            close(t)
            state, since, note = "run", t, ""
        elif kind == "suspend":
            close(t)
            state, since, note = "frozen", t, _note(msg)
        elif kind == "requeue":
            close(t)
            state, since, note = "wait", t, _note(msg)
            marks.append([round(t, 1), "restart", msg])
        elif kind in ("stop", "hung"):
            marks.append([round(t, 1), kind, msg])
        elif kind in TERMINAL:
            close(t)
            state = None
            marks.append([round(t, 1), kind, msg])
    if state is not None:
        close(task["finished_at"] or now)
    if not segs and not events:  # the log of an old task was pruned: its own timestamps are all there is
        sub, start, end = task["submitted_at"], task["started_at"], task["finished_at"] or now
        if start:
            segs = [[sub, start, "wait", ""], [start, end, "run", ""]] if start > sub else [[start, end, "run", ""]]
        else:
            segs = [[sub, end, "wait", ""]]
    return segs, marks


def totals(segs: list) -> dict:
    out = {"run": 0.0, "wait": 0.0, "frozen": 0.0}
    for s0, s1, kind, _ in segs:
        out[kind] += s1 - s0
    return out
