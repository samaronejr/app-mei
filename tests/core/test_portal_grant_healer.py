"""The portal grants must heal themselves on `migrate`, and cost nothing when clean.

`core.E012` refuses to boot when an allow-listed table exists without its portal grant.
It is a backstop, not a fix: `ops/sql/roles.sql` runs from the Postgres init hook
against an EMPTY data directory, so on a fresh cluster it grants nothing at all and
every table a migration creates arrives with RESTRICTIVE policies and no grant. The
backstop then turns the documented `docker compose down -v && up -d --wait` bootstrap
into a refusal nobody can clear without reading the runbook.

`apply_portal_grants` closes that loop by issuing the missing grants from
`post_migrate`, immediately after the migration that created the tables. Three
properties are load-bearing and each has a test below:

* It must actually grant. The first case revokes a real grant on the real database and
  asserts the healer puts it back — with a positive control proving the revoke landed,
  so the assertion cannot pass vacuously.
* It must never abort a migrate. A missing role, a table that does not exist yet, a
  non-PostgreSQL connection and an outright `DatabaseError` all resolve to "do nothing",
  mirroring `core.E012`'s own fail-open posture.
* It must be near-free. `post_migrate` is emitted by `flush` in
  `TransactionTestCase._fixture_teardown` as well as by `migrate`, so this receiver runs
  roughly 470 times per suite. The roles.sql parse is memoised and the missing pairs are
  computed in ONE catalog query, so a clean database costs two round-trips and no GRANT.

Only the first case needs a database. The rest drive the branch logic through a stub
cursor, the style `tests/core/test_portal_role_check.py` established, because the
interesting states (no role, no table) cannot be arranged on a live cluster without
widening `app_test` purely to satisfy a test.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

import pytest
from django.apps import apps as django_apps
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connection
from django.db.models.signals import post_migrate

from apps.core import grants
from apps.core.grants import PORTAL_ROLE, apply_portal_grants

RUNTIME_ROLE = "app_runtime"
VAULT_TABLE = "obligations_document"
DISPATCH_UID = "core.apply_portal_grants"


@contextmanager
def _as_owner() -> Iterator[None]:
    """Drop the suite's `SET ROLE` so statements run as the table OWNER.

    `tests/conftest.py` runs every database test as `app_runtime`, which owns nothing
    and cannot GRANT. The owner of the test database's tables is `app_test`, the login
    role — so resetting is enough. Precedent: `tests/audit/test_append_only.py`.
    """
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"SET ROLE {RUNTIME_ROLE}")


def _portal_has_insert_on_the_vault() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege(%s, to_regclass(%s), %s)",
            [PORTAL_ROLE, f"public.{VAULT_TABLE}", "INSERT"],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


class _StubCursor:
    """Answer the role probe with `fetchone`, the missing-pair query with `fetchall`."""

    def __init__(
        self,
        role_row: tuple[object, ...] | None,
        missing: list[tuple[str, str]],
    ) -> None:
        self.executed: list[str] = []
        self._role_row = role_row
        self._missing = missing

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
        del params
        self.executed.append(sql)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._role_row

    def fetchall(self) -> list[tuple[str, str]]:
        return self._missing

    def granted(self) -> list[str]:
        return [sql for sql in self.executed if sql.startswith("GRANT")]


class _StubConnection:
    vendor = "postgresql"

    def __init__(self, cursor: _StubCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _StubCursor:
        return self._cursor


class _SqliteConnection:
    vendor = "sqlite"

    def cursor(self) -> _StubCursor:  # pragma: no cover - must never be reached
        msg = "the healer opened a cursor on a non-PostgreSQL connection"
        raise AssertionError(msg)


class _BrokenConnection:
    vendor = "postgresql"

    def cursor(self) -> _StubCursor:
        msg = "connection refused"
        raise DatabaseError(msg)


@pytest.fixture
def _clean_grant_cache() -> Iterator[None]:
    """Isolate the memoised roles.sql parse from what a test patches into it."""
    grants._declared_grants.cache_clear()
    yield
    grants._declared_grants.cache_clear()


@pytest.mark.django_db(transaction=True)
def test_the_healer_restores_a_grant_revoked_from_the_real_database() -> None:
    # Given the vault's INSERT grant revoked on the real database, as the table owner
    with _as_owner():
        with connection.cursor() as cursor:
            cursor.execute(f"REVOKE INSERT ON public.{VAULT_TABLE} FROM {PORTAL_ROLE}")

        # Positive control: without this the assertion below would pass on a database
        # where the grant had never been removed, which is every database.
        assert _portal_has_insert_on_the_vault() is False

        try:
            # When the receiver runs
            apply_portal_grants(
                sender=django_apps.get_app_config("core"),
                using=DEFAULT_DB_ALIAS,
            )

            # Then the grant is back, without anybody re-running roles.sql by hand
            assert _portal_has_insert_on_the_vault() is True
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"GRANT INSERT ON public.{VAULT_TABLE} TO {PORTAL_ROLE}",
                )


def test_the_receiver_is_connected_to_post_migrate_for_the_core_app_config() -> None:
    # Given Django's signal registry, populated by CoreConfig.ready
    expected_sender = id(django_apps.get_app_config("core"))

    # When the receiver's dispatch_uid is looked for
    keys = [entry[0] for entry in post_migrate.receivers]

    # Then it is bound to the core AppConfig, so it fires exactly ONCE per migrate
    # rather than once per installed app. A receiver that is never connected heals
    # nothing and looks exactly like a receiver that found nothing to heal.
    assert (DISPATCH_UID, expected_sender) in keys


def test_a_non_postgresql_connection_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a connection whose vendor cannot express these grants at all
    monkeypatch.setattr(
        grants,
        "connections",
        {DEFAULT_DB_ALIAS: _SqliteConnection()},
    )

    # When the receiver runs
    # Then it returns before opening a cursor. The stub raises if one is opened, so a
    # healer that probed first and branched later would fail here.
    apply_portal_grants(sender=None, using=DEFAULT_DB_ALIAS)


def test_an_absent_portal_role_grants_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a cluster that has no app_portal role at all
    cursor = _StubCursor(role_row=None, missing=[])
    monkeypatch.setattr(
        grants,
        "connections",
        {DEFAULT_DB_ALIAS: _StubConnection(cursor)},
    )

    # When the receiver runs
    apply_portal_grants(sender=None, using=DEFAULT_DB_ALIAS)

    # Then it issues no GRANT. Granting to a role that does not exist raises, and a
    # raise here aborts the migrate this receiver is attached to.
    assert cursor.granted() == []


def test_a_table_that_does_not_exist_yet_is_never_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a role that exists and a catalog query that returns no missing pairs --
    # which is what a not-yet-created table produces, because has_table_privilege on a
    # NULL regclass is NULL rather than FALSE
    cursor = _StubCursor(role_row=(1,), missing=[])
    monkeypatch.setattr(
        grants, "connections", {DEFAULT_DB_ALIAS: _StubConnection(cursor)}
    )

    # When the receiver runs
    apply_portal_grants(sender=None, using=DEFAULT_DB_ALIAS)

    # Then nothing is granted, and the whole firing cost two round-trips
    assert cursor.granted() == []
    assert len(cursor.executed) == 2


def test_only_the_pairs_the_catalog_reported_missing_are_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given exactly one missing pair
    cursor = _StubCursor(role_row=(1,), missing=[(VAULT_TABLE, "INSERT")])
    monkeypatch.setattr(
        grants, "connections", {DEFAULT_DB_ALIAS: _StubConnection(cursor)}
    )

    # When the receiver runs
    apply_portal_grants(sender=None, using=DEFAULT_DB_ALIAS)

    # Then that pair is granted and nothing else is. Re-granting the whole allow-list on
    # every firing would be ~470 pointless DDL statements per test suite.
    assert cursor.granted() == [
        f'GRANT INSERT ON public."{VAULT_TABLE}" TO "{PORTAL_ROLE}"',
    ]


def test_a_database_error_never_aborts_the_migrate_it_is_attached_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a database that cannot be queried at all
    monkeypatch.setattr(grants, "connections", {DEFAULT_DB_ALIAS: _BrokenConnection()})

    # When the receiver runs
    # Then it returns rather than raising. post_migrate runs INSIDE the migrate command:
    # a raise here turns a healing failure into a deployment that cannot migrate.
    apply_portal_grants(sender=None, using=DEFAULT_DB_ALIAS)


@pytest.mark.usefixtures("_clean_grant_cache")
def test_a_pair_that_is_not_a_bare_identifier_never_reaches_the_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a roles.sql parse that yielded a table name with punctuation in it and a
    # privilege outside the two this role is ever given
    monkeypatch.setattr(
        grants,
        "_roles_sql_grants",
        lambda: frozenset(
            {
                ("obligations_document; DROP TABLE audit_event", "SELECT"),
                ("obligations_document", "DELETE"),
                ("obligations_document", "INSERT"),
            },
        ),
    )

    # When the declared grants are resolved
    declared = grants._declared_grants()

    # Then only the well-formed pair survives. Table names are interpolated into DDL --
    # identifiers cannot be bound as parameters -- so this filter is the boundary.
    assert declared == ((VAULT_TABLE, "INSERT"),)


@pytest.mark.usefixtures("_clean_grant_cache")
def test_the_roles_sql_parse_happens_once_however_often_the_signal_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a parse we can count
    calls = 0

    def _counted() -> frozenset[tuple[str, str]]:
        nonlocal calls
        calls += 1
        return frozenset({(VAULT_TABLE, "INSERT")})

    monkeypatch.setattr(grants, "_roles_sql_grants", _counted)

    # When the declared grants are resolved as many times as a transactional suite fires
    for _ in range(5):
        grants._declared_grants()

    # Then roles.sql was read and re-parsed exactly once. TransactionTestCase's flush
    # teardown emits post_migrate too, so this runs ~470 times per suite; re-reading and
    # re-regexing the file each time is the difference between free and measurable.
    assert calls == 1
