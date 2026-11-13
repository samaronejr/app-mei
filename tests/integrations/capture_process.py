"""Spawned capture ownership probe; Django starts inside the child."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.connection import Connection


def try_guard(
    token: str,
    directory: str,
    database: dict[str, object],
    pipe: Connection,
    *,
    crash: bool = False,
) -> None:
    """Report admission once; optional process death bypasses Python cleanup."""
    import os  # noqa: PLC0415

    import django  # noqa: PLC0415

    django.setup()
    from django.db import connections  # noqa: PLC0415
    from django.test import override_settings  # noqa: PLC0415

    from apps.integrations.focus.capture_lock import token_guard  # noqa: PLC0415

    connections.databases["default"] = database
    with (
        override_settings(FOCUS_CAPTURE_LOCK_DIR=directory),
        token_guard(token) as guard,
    ):
        pipe.send(guard is not None)
        if crash and guard is not None:
            os._exit(17)
    pipe.close()
