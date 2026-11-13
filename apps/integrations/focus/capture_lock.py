"""Token ownership survives a lost database connection in the deployed worker.

The deployment has one worker container on one host. Its processes share the lock
directory: a process-lifetime flock stays held when the dedicated PostgreSQL
session disappears, and process death releases both. The advisory session also
arbitrates intact workers. Neither guard expires while its owner is alive.
Only an opaque HMAC scope is written; the credential stays in process memory.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections
from django.utils import timezone
from redis import Redis

from apps.core.tasks import assert_no_transaction

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper
    from psycopg import Connection

_HELD: Final = """
WITH k AS (SELECT hashtextextended(%s, 0) AS value)
SELECT EXISTS (
    SELECT 1 FROM pg_locks l, k
    WHERE l.locktype = 'advisory' AND l.pid = pg_backend_pid() AND l.granted
      AND l.objsubid = 1
      AND l.classid::bigint = ((k.value >> 32) & 4294967295)
      AND l.objid::bigint = (k.value & 4294967295)
)
"""
_ADMIT: Final = """
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)
if redis.call('ZCARD', KEYS[1]) >= 60 then return 0 end
redis.call('ZADD', KEYS[1], now, ARGV[2])
redis.call('PEXPIRE', KEYS[1], 60000)
return 1
"""


class CaptureLockLostError(Exception):
    """Refuse an owner that no longer proves its original advisory session."""


def token_scope(token: str) -> str:
    """Derive an opaque, non-reversible quota/lock name, never a credential."""
    return hmac.new(
        settings.SECRET_KEY.encode(), token.encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TokenGuard:
    """The exact dedicated backend must still own this scope; never reconnect."""

    connection: BaseDatabaseWrapper = field(repr=False)
    original: Connection[tuple[object, ...]] = field(repr=False)
    key: str
    token: str = field(repr=False)

    def require_owned(self) -> None:
        """Refuse a lost or replaced session before a send or page commit."""
        held = False
        if self.connection.connection is self.original and not self.original.closed:
            try:
                with self.connection.cursor() as cursor:
                    cursor.execute(_HELD, [self.key])
                    row = cursor.fetchone()
                    held = bool(row and row[0])
            except DatabaseError:
                held = False
        if not held:
            raise CaptureLockLostError


@contextmanager
def token_guard(token: str) -> Iterator[TokenGuard | None]:
    """Try once, with no transaction spanning HTTP; close both handles on exit."""
    assert_no_transaction()
    scope = token_scope(token)
    directory = Path(
        getattr(
            settings,
            "FOCUS_CAPTURE_LOCK_DIR",
            str(Path(tempfile.gettempdir()) / "axorim-focus-capture"),
        )
    )
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(
        directory / scope,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    lock = None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        lock = connections.create_connection(DEFAULT_DB_ALIAS)
        lock.ensure_connection()
        lock.set_autocommit(True)
        key = f"focus-capture:{scope}"
        with lock.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [key]
            )
            acquired = cursor.fetchone()
        if not acquired or not acquired[0]:
            yield None
            return
        yield TokenGuard(lock, lock.connection, key, token)
    finally:
        try:
            if lock is not None:
                lock.close()
        finally:
            os.close(descriptor)


class SharedTokenRateLimiter:
    """Exact shared 60/minute send window, including sequential process handoffs."""

    def __init__(self) -> None:
        """Use the existing broker; store only opaque scopes, ids and timestamps."""
        self.client = Redis.from_url(settings.REDIS_URL)
        self.script = self.client.register_script(_ADMIT)

    def acquire(self, token: str) -> bool:
        """Atomically admit one send; unavailable Redis raises before any HTTP."""
        assert_no_transaction()
        accepted = self.script(
            keys=[f"focus:capture:quota:{token_scope(token)}"],
            args=[int(timezone.now().timestamp() * 1000), uuid4().hex],
        )
        return isinstance(accepted, int) and accepted == 1
