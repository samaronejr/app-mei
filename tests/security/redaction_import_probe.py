"""Fresh-process witness for a private Celery name disappearing before import."""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import TYPE_CHECKING

import django
import sentry_sdk
from celery import _state
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.transport import Transport

if TYPE_CHECKING:
    from sentry_sdk.envelope import Envelope


class _Capture(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[bytes] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.payloads.append(envelope.serialize())


def main() -> None:
    django.setup()
    # Preload the SDK integration, not the production settings being tested.
    CeleryIntegration()
    assert "config.settings.prod" not in sys.modules
    del _state._task_stack
    from config.settings import prod  # noqa: PLC0415 - the import is the action

    canary = "synthetic-import-rename-credential-80164"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(prod.CredentialURLLogFilter())
    transport = _Capture()
    options = prod.SENTRY_OPTIONS.copy()
    options.update(
        {
            "dsn": "http://public@example.invalid/1",
            "transport": transport,
        }
    )
    client = sentry_sdk.Client(**options)
    try:
        with sentry_sdk.isolation_scope() as scope, sentry_sdk.new_scope():
            scope.set_client(client)
            handler.handle(logging.makeLogRecord({"msg": canary}))
            sentry_sdk.capture_message(canary)
            client.flush()
    finally:
        client.close()
        handler.close()
    sys.stdout.write(
        json.dumps({"safe": canary not in stream.getvalue() and not transport.payloads})
    )


if __name__ == "__main__":
    main()
