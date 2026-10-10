"""Task progress: written by the task itself to RMQ_PROGRESS_FILE, or read from a tqdm bar in its log.

The progress file holds one line: "done/total [note]" (e.g. "37/120 epoch 3"), "NN% [note]", or JSON
{"done": 37, "total": 120, "note": "epoch 3"} / {"percent": 45}.
"""
from __future__ import annotations

import json
import re

_FRACTION = re.compile(r"^(\d+(?:[.,]\d+)?)\s*/\s*(\d+(?:[.,]\d+)?)\s*(.*)$", re.S)
_PERCENT = re.compile(r"^(\d+(?:[.,]\d+)?)\s*%\s*(.*)$", re.S)
# tqdm: " epoch 3:  45%|████▌     | 45/100 [00:10<00:12,  4.50it/s]" (counts may be scaled: 1.2k/10.0k)
_TQDM = re.compile(r"(\d{1,3})%\|[^|\r\n]{0,300}\|\s*(\d+(?:\.\d+)?)([kMGT]?)/(\d+(?:\.\d+)?)([kMGT]?)")
_SCALE = {"": 1.0, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def _result(done: float | None, total: float | None, pct: float | None, note: str) -> dict | None:
    if pct is None:
        if done is None or not total or total <= 0:
            return None
        pct = done / total * 100.0
    if pct < 0:
        return None
    return {"done": done, "total": total, "pct": round(min(100.0, pct), 1), "note": " ".join(note.split())[:120]}


def parse_explicit(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            d = json.loads(text)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        try:
            done = float(d["done"]) if d.get("done") is not None else None
            total = float(d["total"]) if d.get("total") is not None else None
            pct = float(d["percent"]) if d.get("percent") is not None else None
        except (TypeError, ValueError):
            return None
        return _result(done, total, pct, str(d.get("note", "")))
    m = _FRACTION.match(text)
    if m:
        return _result(_num(m.group(1)), _num(m.group(2)), None, m.group(3))
    m = _PERCENT.match(text)
    if m:
        return _result(None, None, _num(m.group(1)), m.group(2))
    return None


def parse_tqdm(tail: str) -> dict | None:
    """The last tqdm bar in a piece of log (the innermost bar when several are nested)."""
    last = None
    for last in _TQDM.finditer(tail):
        pass
    if last is None:
        return None
    done = float(last.group(2)) * _SCALE[last.group(3)]
    total = float(last.group(4)) * _SCALE[last.group(5)]
    start = max(tail.rfind("\r", 0, last.start()), tail.rfind("\n", 0, last.start())) + 1
    desc = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", tail[start:last.start()]).strip().rstrip(":").strip()
    return _result(done, total, None if total > 0 else float(last.group(1)), desc)
