from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_Z = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z$")
_DUR = re.compile(r"^(\d+)([smh])$")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc = dt.astimezone(timezone.utc).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_z(s: str) -> datetime:
    m = _Z.match(s)
    if not m:
        raise ValueError(f"expected UTC Z timestamp, got {s!r}")
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def add_seconds(dt: datetime, n: int) -> datetime:
    return dt + timedelta(seconds=n)


def parse_duration(value: str | int) -> int:
    if isinstance(value, int):
        if value < 0:
            raise ValueError("duration must be >= 0")
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    m = _DUR.match(str(value))
    if not m:
        raise ValueError(f"invalid duration {value!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600}[unit]
