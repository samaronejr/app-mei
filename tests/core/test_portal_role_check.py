"""`core.E008` must reject the two ways the portal role goes silently wrong.

Both failures produce no error anywhere else, in opposite directions:

* A missing `app_portal` means `SET LOCAL ROLE` raises on the first portal request —
  loud, but only once a portal user exists to hit it.
* An INHERITING grant is worse and entirely silent. PostgreSQL matches a policy's `TO`
  clause by privilege inheritance rather than identity, so `app_runtime` acquires the
  RESTRICTIVE portal policies and the accounting firm reads zero rows from every client
  table. Nothing raises; the tables simply look empty.

The branch logic is driven through a stub cursor rather than by re-granting the real
role. Grants are cluster-global and require ADMIN OPTION, which the test login
deliberately does not hold — arranging for it would widen `app_test` purely to satisfy
a test. The check was falsified against a live cluster instead, recorded in
`.evidence/T-051-failure.txt`: `GRANT ... WITH INHERIT TRUE` made `manage.py check`
report core.E008, and `WITH INHERIT FALSE` cleared it.

The first test below queries the real cluster, so this module still fails if the
deployed grant is ever wrong.
"""

from types import TracebackType
from typing import Self

import pytest
from django.db import DatabaseError

from apps.core import checks
from apps.core.checks import check_portal_role

pytestmark = pytest.mark.django_db(transaction=True)


class _StubCursor:
    """Answer the two queries `_portal_role_errors` issues, in order."""

    def __init__(self, answers: list[tuple[object, ...] | None]) -> None:
        self._answers = answers
        self._calls = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str, params: list[object] | None = None) -> None:
        del sql, params

    def fetchone(self) -> tuple[object, ...] | None:
        answer = self._answers[self._calls]
        self._calls += 1
        return answer


class _StubConnection:
    def __init__(self, answers: list[tuple[object, ...] | None]) -> None:
        self._answers = answers

    def cursor(self) -> _StubCursor:
        return _StubCursor(self._answers)


class _BrokenConnection:
    def cursor(self) -> _StubCursor:
        msg = "connection refused"
        raise DatabaseError(msg)


def _ids() -> set[str | None]:
    return {message.id for message in check_portal_role(None)}


def test_the_check_passes_against_the_real_bootstrapped_cluster() -> None:
    # Given the cluster ops/sql/roles.sql produced, queried for real
    # When the check runs
    # Then nothing is reported. This is the case that fails if the DEPLOYED grant is
    # ever wrong, which the stub-driven cases below cannot see.
    assert check_portal_role(None) == []


def test_a_missing_portal_role_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a cluster where the role does not exist
    monkeypatch.setattr(checks, "connections", {"default": _StubConnection([(False,)])})

    # When the check runs
    # Then core.E008 fires, rather than the failure surfacing later at SET LOCAL ROLE
    assert "core.E008" in _ids()


def test_an_inheriting_grant_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a role that exists but is inherited by app_runtime
    monkeypatch.setattr(
        checks,
        "connections",
        {"default": _StubConnection([(True,), (True,)])},
    )

    # When the check runs
    # Then core.E008 fires. Without it the firm silently reads nothing, and the first
    # report is a customer rather than a build.
    assert "core.E008" in _ids()


def test_a_correct_grant_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a role that exists and is NOT inherited
    monkeypatch.setattr(
        checks,
        "connections",
        {"default": _StubConnection([(True,), (False,)])},
    )

    # When the check runs
    # Then it is silent, so the two cases above cannot be passing for a trivial reason
    assert check_portal_role(None) == []


def test_an_unreachable_database_does_not_break_manage_py(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a database that cannot be queried
    monkeypatch.setattr(checks, "connections", {"default": _BrokenConnection()})

    # When the check runs
    # Then it reports nothing. This is the only DB-touching check in the project and
    # manage.py must stay usable without a database; CI runs `check` against a live,
    # role-bootstrapped cluster, which is where it bites.
    assert check_portal_role(None) == []
