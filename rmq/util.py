"""Small pure helpers shared by the server and the CLI (stdlib only)."""
from __future__ import annotations

import re

GB = 1024 ** 3

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)(min|sec|hr|ms|s|m|h|d)?")
_DUR_UNITS = {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400, "ms": 0.001}


def parse_duration(value) -> float:
    """'90', '90s', '15m', '2h', '1d', '1h30m' -> seconds."""
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("duration must be >= 0")
        return float(value)
    s = str(value).strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty duration")
    pos, total = 0, 0.0
    for m in _DUR_RE.finditer(s):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _DUR_UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s):
        raise ValueError(f"bad duration {value!r}; examples: 90s, 15m, 2h, 1d, 1h30m")
    return total


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)(k|kb|m|mb|g|gb|t|tb)?$")
_SIZE_TO_GB = {None: 1, "g": 1, "gb": 1, "m": 1 / 1024, "mb": 1 / 1024,
               "k": 1 / 1024 ** 2, "kb": 1 / 1024 ** 2, "t": 1024, "tb": 1024}


def parse_size_gb(value) -> float:
    """'8', '8G', '512M', '1.5gb' -> GB (binary, as Windows shows)."""
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("size must be >= 0")
        return float(value)
    m = _SIZE_RE.match(str(value).strip().lower().replace(" ", ""))
    if not m:
        raise ValueError(f"bad size {value!r}; examples: 8, 8G, 512M")
    return float(m.group(1)) * _SIZE_TO_GB[m.group(2)]


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(max(0.0, seconds)))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m:02d}м"
    if m:
        return f"{m}м {s:02d}с"
    return f"{s}с"


PRIORITIES = {"low": 0, "normal": 1, "high": 2}
PRIORITY_NAMES = {v: k for k, v in PRIORITIES.items()}


def parse_priority(value) -> int:
    if isinstance(value, int) and value in PRIORITY_NAMES:
        return value
    s = str(value).strip().lower()
    if s.isdigit() and int(s) in PRIORITY_NAMES:
        return int(s)
    if s not in PRIORITIES:
        raise ValueError("priority must be low, normal or high")
    return PRIORITIES[s]
