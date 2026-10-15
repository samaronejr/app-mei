"""AXR1-037: the investigation deletes every session it issued, even mid-preparation."""

from __future__ import annotations

from argparse import Namespace
from typing import TYPE_CHECKING, Self

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session

from apps.core.tenancy import tenant_context
from ops.perf import hoje_investigation, seats
from tests.briefing.http_fixtures import http_firm

if TYPE_CHECKING:
    from pathlib import Path

    from apps.accounts.models import User

PARTIAL_COOKIE = "sessionid=partial-owner-key"


class _Cursor:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, _sql: str) -> None:
        return None

    def fetchone(self) -> tuple[str, bool, bool]:
        return ("app_runtime", False, False)


class _Connection:
    def cursor(self) -> _Cursor:
        return _Cursor()

    def close(self) -> None:
        return None


def test_partial_target_preparation_still_removes_issued_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    removed: list[list[str]] = []

    def prepare(_slug: str, issued: list[str] | None = None) -> tuple[()]:
        if issued is not None:
            issued.append(PARTIAL_COOKIE)
        message = "portal preparation failed after owner sessions"
        raise RuntimeError(message)

    def remove(cookies: list[str]) -> int:
        removed.append(list(cookies))
        return len(cookies)

    arguments = Namespace(
        head="h", output=tmp_path / "out.json", requests=200, explain=3
    )
    monkeypatch.setattr(hoje_investigation, "_arguments", lambda: arguments)
    monkeypatch.setattr(
        hoje_investigation, "production_memory_limits", lambda _path: {"web": 1}
    )
    monkeypatch.setattr(
        hoje_investigation, "constrained_resources", lambda _path, _limit: {}
    )
    monkeypatch.setattr(hoje_investigation, "boundary_inputs", dict)
    monkeypatch.setattr(hoje_investigation, "connection", _Connection())
    monkeypatch.setattr(hoje_investigation, "_dataset", lambda _slug: {})
    monkeypatch.setattr(hoje_investigation, "_write", lambda _output, _report: None)
    monkeypatch.setattr(hoje_investigation, "read_targets", prepare)
    monkeypatch.setattr(
        hoje_investigation, "remove_cookie_sessions", remove, raising=False
    )

    with pytest.raises(RuntimeError, match="after owner sessions"):
        hoje_investigation.main()

    assert removed == [[PARTIAL_COOKIE]]


@pytest.mark.django_db
def test_each_issued_session_is_reported_before_a_later_actor_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = http_firm()
    unrelated = SessionStore()
    unrelated.create()
    real_cookie = seats.session_cookie
    calls: list[int] = []

    def cookie_then_fail(user: User) -> str:
        calls.append(1)
        if len(calls) == 3:
            message = "third actor could not be prepared"
            raise RuntimeError(message)
        return real_cookie(user)

    monkeypatch.setattr(seats, "ACTORS_PER_ROLE", 3)
    monkeypatch.setattr(seats, "session_cookie", cookie_then_fail)
    issued: list[str] = []

    with tenant_context(firm.tenant.pk), pytest.raises(RuntimeError, match="third"):
        seats._actors(firm.tenant, None, issued)

    keys = [cookie.split("=", 1)[1] for cookie in issued]
    assert len(keys) == 2
    assert Session.objects.filter(session_key__in=keys).count() == 2
    assert seats.remove_cookie_sessions(issued) == 2
    assert not Session.objects.filter(session_key__in=keys).exists()
    assert Session.objects.filter(session_key=unrelated.session_key).exists()
