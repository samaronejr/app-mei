"""Transient redaction inputs, never attached to a task argument or log record."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

sensitive_values: ContextVar[tuple[str, ...]] = ContextVar(
    "delivery_sensitive_values", default=()
)


@contextmanager
def delivery_secrets(*values: str) -> Iterator[None]:
    """Allow console and Sentry scrubbers to redact aliases in exception text."""
    token = sensitive_values.set(tuple(value for value in values if value))
    try:
        yield
    finally:
        sensitive_values.reset(token)
