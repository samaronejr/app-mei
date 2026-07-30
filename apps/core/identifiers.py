"""A process-local lock around the UUIDv7 generator.

`uuid6.uuid7()` keeps a module-global `_last_v7_timestamp` and advances it with a
non-atomic read-modify-write, holding no lock of its own. Two threads can interleave
inside that sequence, so under a threaded worker the update is unsynchronised.

What the race can cost is **monotonic ordering, not uniqueness**. A duplicate would
need the 48-bit millisecond field *and* the 76 bits from `secrets.randbits(76)` to
agree. Measured on this codebase at 16 threads x 4000 generations: zero duplicates and
zero repeated timestamps -- which shows the race is rare, not that it is absent, and
rarity is not a guarantee. The lock removes the question rather than betting on it.

UUIDv7 ordering is an **index-locality property here, never a business ordering
guarantee**: no caller may infer sequence, causality, or a timestamp from a primary
key. Sort on an explicit column.

The lock is process-local because that is exactly the scope of the state it guards --
`_last_v7_timestamp` is a module global, so one lock per process is sufficient and a
cross-process lock would guard nothing extra. gunicorn runs `gthread` with 2 workers x
2 threads, so the contended case is 2 threads contending on a call measured in
microseconds. Celery's prefork pool is process-based and needs no coordination here.
"""

import threading
from uuid import UUID

import uuid6

_LOCK = threading.Lock()


def uuid7() -> UUID:
    """Return a UUIDv7, serialising `uuid6`'s unguarded counter update."""
    with _LOCK:
        return uuid6.uuid7()
