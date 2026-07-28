"""Test-suite wiring for the database role split.

Two things happen here, and both are load-bearing for every isolation assertion in
this project:

1. After migrations, `app_runtime` is granted DML on the freshly created test tables.
   Neither `ALTER DEFAULT PRIVILEGES` nor the main database's ACLs reach them — test
   databases are built from `template1` and their tables are owned by `app_test` — so
   without this the suite dies with `permission denied` before reaching a policy.
2. Every database test runs its assertions under `SET ROLE app_runtime`. The login
   role is `app_test`, which owns the tables and holds `BYPASSRLS`; leaving it in place
   would let every cross-tenant test pass while reading every tenant's rows.
"""

import importlib
from collections.abc import Iterator

import pytest
from django.apps import apps as django_apps
from django.core.cache import cache
from django.db import connection
from pytest_django import DjangoDbBlocker

RUNTIME_ROLE = "app_runtime"

# Tables whose migrations revoke write access. Listed here rather than discovered so
# that a new append-only table which forgets its revoke is a visible omission.
APPEND_ONLY_TABLES = ("audit_event", "audit_platformevent")


@pytest.fixture(scope="session")
def django_db_setup(
    django_db_setup: None,
    django_db_blocker: DjangoDbBlocker,
) -> None:
    """Grant the runtime role access to the tables migrations just created.

    `ALTER DEFAULT PRIVILEGES` only affects objects created *afterwards*, so the
    statement in `ops/sql/roles.sql` is inert for tables that already exist. The
    explicit `ON ALL TABLES` grant below is what actually lands.
    """
    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}",
        )
        cursor.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}",
        )
        # The blanket grant above would silently restore UPDATE/DELETE on the
        # append-only audit tables, so the revoke is re-applied as the last statement.
        # Note the grant above enumerates privileges rather than using ALL: a blanket
        # GRANT ALL would also hand back TRUNCATE, which is a privilege of its own,
        # is documented as NOT subject to row-level security, and would empty an
        # append-only table without firing a single row trigger.
        for table in APPEND_ONLY_TABLES:
            cursor.execute(
                f"""
                DO $$
                BEGIN
                    IF to_regclass('public.{table}') IS NOT NULL THEN
                        REVOKE ALL ON {table} FROM {RUNTIME_ROLE};
                        GRANT SELECT, INSERT ON {table} TO {RUNTIME_ROLE};
                    END IF;
                END
                $$
                """,
            )


def _wants_database(request: pytest.FixtureRequest) -> bool:
    if request.node.get_closest_marker("django_db") is not None:
        return True
    return bool({"db", "transactional_db"} & set(request.fixturenames))


@pytest.fixture(autouse=True)
def _assume_runtime_role(request: pytest.FixtureRequest) -> Iterator[None]:
    """Run each database test's assertions as the unprivileged application role.

    Requesting `db` rather than declaring it as a parameter keeps non-database tests
    off the database entirely, while still ordering this fixture *after* pytest-django
    has flushed and *before* it flushes again on teardown. That ordering is required:
    `TransactionTestCase` truncates as the current role, and `app_runtime` cannot
    truncate tables owned by `app_test`.
    """
    if not _wants_database(request):
        yield
        return

    request.getfixturevalue("db")
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {RUNTIME_ROLE}")
    try:
        yield
    finally:
        if connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")


@pytest.fixture(autouse=True)
def _seeded_capability_matrix(request: pytest.FixtureRequest) -> None:
    """Restore the permission matrix that a transactional test truncates away.

    `authz_capability` and `authz_rolegrant` are reference data written by a data
    migration, so every real deployment has them. `TransactionTestCase` truncates every
    table and does not replay `RunPython`, which leaves `can()` resolving against an
    empty matrix — and an empty matrix denies everything, silently, in whatever test
    happens to run next. Restoring it here makes the test database represent production
    rather than an artifact of the test runner.

    The data migration's own seed function is reused rather than reimplemented, so the
    matrix can never be seeded one way by migrate and another way by the suite. It is
    idempotent, and the existence check keeps the common case to one query.
    """
    if not _wants_database(request):
        return
    request.getfixturevalue("db")
    capability = django_apps.get_model("authz", "Capability")
    if capability.objects.exists():
        return
    migration = importlib.import_module("apps.authz.migrations.0002_seed_matrix")
    migration.seed(django_apps, None)


@pytest.fixture(autouse=True)
def _seeded_municipalities(request: pytest.FixtureRequest) -> None:
    """Restore the municipality registry that a transactional test truncates away.

    Same reasoning as the capability matrix above: `fiscal_municipality` is platform
    reference data written by a data migration, so every real deployment has it, and
    `TransactionTestCase` truncates it without replaying `RunPython`. Left empty, every
    municipality lookup would answer `unknown` — which is a *valid* answer, so the
    resulting tests would pass while asserting nothing about the seeded path.

    The data migration's own seed function is reused rather than reimplemented.
    """
    if not _wants_database(request):
        return
    request.getfixturevalue("db")
    municipality = django_apps.get_model("fiscal", "Municipality")
    if municipality.objects.exists():
        return
    migration = importlib.import_module("apps.fiscal.migrations.0002_seed_capitals")
    migration.seed_from_global_registry()


@pytest.fixture(autouse=True)
def _seeded_fiscal_parameters(request: pytest.FixtureRequest) -> None:
    """Restore the fiscal parameters that a transactional test truncates away.

    Same reasoning as the two fixtures above, with a sharper edge. The obligation
    engine reads every limit and deadline from this table and `parameter_for` raises
    `NoEffectiveParameter` rather than defaulting, so an empty table turns every
    threshold and due-date test into an error about missing seed data — and a test
    written to assert that raise would pass while exercising nothing.

    The data migration's own seed function is reused rather than reimplemented, so
    the table can never be filled one way by migrate and another way by the suite.
    """
    if not _wants_database(request):
        return
    request.getfixturevalue("db")
    parameter = django_apps.get_model("obligations", "FiscalParameter")
    if parameter.objects.exists():
        return
    migration = importlib.import_module(
        "apps.obligations.migrations.0002_seed_2026_parameters",
    )
    migration.seed_from_global_registry()


@pytest.fixture(autouse=True)
def _seeded_obligation_types(request: pytest.FixtureRequest) -> None:
    """Restore the obligation types that a transactional test truncates away.

    Without them every due-date test raises `ObligationType.DoesNotExist`, and the
    generator has nothing to generate — which reads as "no obligations are due",
    the one answer this product must never produce by accident.
    """
    if not _wants_database(request):
        return
    request.getfixturevalue("db")
    obligation_type = django_apps.get_model("obligations", "ObligationType")
    if obligation_type.objects.exists():
        return
    migration = importlib.import_module(
        "apps.obligations.migrations.0003_seed_obligation_types",
    )
    migration.seed_from_global_registry()


@pytest.fixture(autouse=True)
def _seeded_national_holidays(request: pytest.FixtureRequest) -> None:
    """Restore the holiday calendar that a transactional test truncates away.

    An empty holiday table does not raise: it answers "every weekday is a business
    day". That is a valid-looking answer and a wrong one, so a due-date test would
    pass while asserting nothing about the calendar it exists to exercise.
    """
    if not _wants_database(request):
        return
    request.getfixturevalue("db")
    holiday = django_apps.get_model("obligations", "Holiday")
    if holiday.objects.exists():
        return
    migration = importlib.import_module(
        "apps.obligations.migrations.0005_seed_national_holidays",
    )
    migration.seed_from_global_registry()


@pytest.fixture(autouse=True)
def _seeded_checklist_templates(request: pytest.FixtureRequest) -> None:
    """Restore the onboarding templates that a transactional test truncates away.

    `create_default_checklist` copies from this table on every client registration,
    and an empty table does not raise: it produces a client with an empty checklist.
    That reads as "nothing is blocked" — which is a valid-looking answer, so the
    onboarding queue would report all clear while asserting nothing at all.

    The data migration's own seed function is reused, then `0005`'s follow-up is
    replayed so the two items that require e-CAC access keep saying so.
    """
    if not _wants_database(request):
        return
    request.getfixturevalue("db")
    template = django_apps.get_model("clients", "OnboardingItemTemplate")
    if template.objects.exists():
        return
    seed_migration = importlib.import_module(
        "apps.clients.migrations.0004_seed_checklist_templates",
    )
    seed_migration.seed(django_apps, None)
    ecac_migration = importlib.import_module(
        "apps.clients.migrations.0005_readiness_and_govbr_trust",
    )
    ecac_migration.mark_ecac_items(django_apps, None)


@pytest.fixture(autouse=True)
def _armed_scheduler_heartbeat(request: pytest.FixtureRequest) -> None:
    """Restore the heartbeat row that a transactional test truncates away.

    Without it `/healthz` answers 503 for every test that follows one, because a
    missing row is deliberately read as "beat has never started" rather than as
    "not applicable".
    """
    if not _wants_database(request):
        return
    request.getfixturevalue("db")
    heartbeat = django_apps.get_model("obligations", "SchedulerHeartbeat")
    if heartbeat.objects.exists():
        return
    migration = importlib.import_module(
        "apps.obligations.migrations.0007_arm_scheduler_heartbeat",
    )
    migration.arm(django_apps, None)


@pytest.fixture(autouse=True)
def _empty_rate_limit_buckets() -> Iterator[None]:
    """Start every test with empty rate-limit buckets.

    The cache is in-process and outlives an individual test, so without this a suite
    that logs in repeatedly would start returning 429 partway through and the failure
    would look like a bug in whatever test happened to be running.
    """
    cache.clear()
    yield
    cache.clear()
