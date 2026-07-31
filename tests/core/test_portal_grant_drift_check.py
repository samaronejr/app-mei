"""`core.E012` must reject a database whose portal grants drifted from roles.sql.

`ops/sql/roles.sql` runs from the Postgres init hook against an **empty** data
directory, so its `to_regclass`-guarded grants reach only tables that already exist.
`migrate` runs afterwards. Every table created by a migration therefore ends up with
its RESTRICTIVE policies and no grant at all — on the documented `docker compose down
-v && up -d --wait` path just as much as on staging.

That is not hypothetical. The document vault was deployed with neither SELECT nor
INSERT on `obligations_document` and would have failed `permission denied` on the first
real upload. The suite could not see it: `tests/conftest.py` issues those grants to the
test database itself, so the tested database was correct while the deployed one was not.

These tests therefore guard two different things, and the second matters more than the
first. One is the branch logic, driven through a stub. The other is that the check can
still *see* anything at all — `_roles_sql_grants` returning an empty set makes
`core.E012` pass vacuously, which is the same silent-success failure the check exists
to stop.
"""

from types import TracebackType
from typing import Self

import pytest
from django.conf import settings
from django.core.checks import registry as checks_registry
from django.db import DatabaseError

from apps.core import checks
from apps.core.checks import (
    PORTAL_GRANT_LOOPS,
    _roles_sql_grants,
    check_portal_grant_drift,
)

pytestmark = pytest.mark.django_db(transaction=True)

_TABLE_EXISTS = ("public.some_table",)
_TABLE_ABSENT = (None,)


class _StubCursor:
    """Answer `to_regclass` then `has_table_privilege`, in the order the check asks."""

    def __init__(self, answers: list[tuple[object, ...]]) -> None:
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

    def fetchone(self) -> tuple[object, ...]:
        answer = self._answers[self._calls]
        self._calls += 1
        return answer


class _StubConnection:
    def __init__(self, answers: list[tuple[object, ...]]) -> None:
        self._answers = answers

    def cursor(self) -> _StubCursor:
        return _StubCursor(self._answers)


class _BrokenConnection:
    def cursor(self) -> _StubCursor:
        msg = "connection refused"
        raise DatabaseError(msg)


def _one_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the allow-list to a single pair so the stub's answers are deterministic."""
    monkeypatch.setattr(
        checks,
        "_roles_sql_grants",
        lambda: frozenset({("some_table", "SELECT")}),
    )


def _ids() -> set[str | None]:
    return {message.id for message in check_portal_grant_drift(None)}


def test_the_check_passes_against_the_real_migrated_database() -> None:
    # Given the database migrations and conftest's grants actually produced
    # When the check runs against it for real
    # Then nothing is reported. This is the case that fails if a future migration adds
    # an allow-listed table and its grant is never issued.
    assert check_portal_grant_drift(None) == []


def test_the_loop_names_are_pinned_against_roles_sql() -> None:
    # Given the production artifact
    source = settings.BASE_DIR / "ops" / "sql" / "roles.sql"
    text = source.read_text(encoding="utf-8")

    # When the loop names this check greps for are looked up in it
    # Then every one is present. Renaming a FOREACH variable in roles.sql without
    # updating PORTAL_GRANT_LOOPS would make _roles_sql_grants return nothing and
    # core.E012 pass on every database forever -- failing open, silently, which is the
    # exact shape of the bug this check was written to catch.
    missing = [
        name for name in PORTAL_GRANT_LOOPS if f"FOREACH {name} IN ARRAY" not in text
    ]
    assert missing == [], f"roles.sql no longer has grant loops named {missing}"


def test_the_allow_list_is_not_empty_and_covers_the_vault() -> None:
    # Given roles.sql parsed by the check itself
    granted = _roles_sql_grants()

    # When the document table is looked for
    # Then both privileges are present. A non-empty assertion alone would still pass if
    # the regex silently matched one loop and not the other.
    assert ("obligations_document", "SELECT") in granted
    assert ("obligations_document", "INSERT") in granted


def test_the_check_is_registered_with_django() -> None:
    # Given Django's check registry, populated by CoreConfig.ready
    registered = checks_registry.registry.get_checks()

    # When the drift check is looked for
    # Then it is there. A check that is never registered reports nothing and looks
    # exactly like a check that found nothing.
    assert check_portal_grant_drift in registered


def test_a_missing_grant_on_an_existing_table_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a table that exists but which the portal role cannot read
    _one_pair(monkeypatch)
    monkeypatch.setattr(
        checks,
        "connections",
        {"default": _StubConnection([_TABLE_EXISTS, (False,)])},
    )

    # When the check runs
    # Then core.E012 fires at boot, rather than the portal meeting permission denied on
    # the first upload a paying customer attempts
    assert "core.E012" in _ids()


def test_a_table_that_does_not_exist_yet_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an allow-listed table no migration has created yet
    _one_pair(monkeypatch)
    monkeypatch.setattr(
        checks,
        "connections",
        {"default": _StubConnection([_TABLE_ABSENT])},
    )

    # When the check runs
    # Then it is silent. On a fresh cluster collectstatic runs system checks before
    # migrate has created anything, so failing here would deadlock the very bootstrap
    # this check protects.
    assert check_portal_grant_drift(None) == []


def test_a_correctly_granted_table_reports_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a table that exists and carries its grant
    _one_pair(monkeypatch)
    monkeypatch.setattr(
        checks,
        "connections",
        {"default": _StubConnection([_TABLE_EXISTS, (True,)])},
    )

    # When the check runs
    # Then it is silent, so the rejection above cannot be passing for a trivial reason
    assert check_portal_grant_drift(None) == []


def test_an_empty_allow_list_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a roles.sql that could not be read or parsed
    monkeypatch.setattr(checks, "_roles_sql_grants", frozenset)

    # When the check runs
    # Then it reports nothing rather than claiming every table is ungranted. This is a
    # deliberate fail-open, and it is only tolerable because the pin above proves the
    # parse still works against the real artifact.
    assert check_portal_grant_drift(None) == []


def test_an_unreachable_database_does_not_break_manage_py(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a database that cannot be queried
    _one_pair(monkeypatch)
    monkeypatch.setattr(checks, "connections", {"default": _BrokenConnection()})

    # When the check runs
    # Then it reports nothing. manage.py must stay usable without a database; the real
    # bite is in CI and in the container, which run `check` against a live cluster.
    assert check_portal_grant_drift(None) == []
