from __future__ import annotations

import os
from datetime import datetime, timezone

_ALPH = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(n: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_ALPH[n & 31])
        n >>= 5
    if n:
        raise ValueError("value too large for ULID field")
    return "".join(reversed(chars))


def new_ulid(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    ms = int(now.timestamp() * 1000)
    if ms < 0 or ms >= (1 << 48):
        raise ValueError("time out of ULID range")
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ms, 10) + _encode(rand, 16)


def new_task_id(now: datetime | None = None) -> str:
    return "task_" + new_ulid(now)


def new_claim_id(now: datetime | None = None) -> str:
    return "clm_" + new_ulid(now)


def new_msg_id(now: datetime | None = None) -> str:
    return "msg_" + new_ulid(now)
