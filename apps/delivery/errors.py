"""Typed failures never contain message content or recipient addresses."""


class IntentConflict(Exception):  # noqa: N818 - public contract name
    """An existing business intent has a different exact-effect digest."""


class VersionConflict(Exception):  # noqa: N818 - shared service vocabulary
    """The operator submitted a stale version; no writes were made."""


class StaleFenceError(Exception):
    """A lease no longer authorizes this worker to send or finalize."""


class ReminderSweepError(RuntimeError):
    """One tenant phase failed; the original exception is retained as its cause."""
