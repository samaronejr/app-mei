"""The backfill data migration: one profile and one engagement per client, per tenant.

S12.1's rules, asserted against the historical model state so the migration itself is
what is being measured — not the current ORM. `is_mei` becomes a `mei` TaxProfile
carrying the legacy category and `valid_from = opened_on or created_at::date`; the
absence of the flag becomes `unknown` rather than a guessed regime, because the old
schema could not say. Every client gains an engagement (`mei_mensal` or
`documentos_mensal`), `provenance='backfill_v1'` marks both, and the assertions inside
the migration hold the created count equal to the source count, per tenant.

The executor plumbing needs explaining, because both choices are load-bearing:

* `RESET ROLE` puts the connection back on `app_test`, the test database's owner —
  which holds BYPASSRLS exactly like the `app_migrator` role that runs this
  migration in production. Under `app_runtime` the backfill would read zero rows
  behind the tenant policy and its own count assertions would never fire, which is
  the silent-empty-backfill failure this migration exists to refuse. The same is
  true for the assertions below: they run while the role is still app_test, because
  app_runtime with no app.tenant_id set would read zero rows through RLS.
* `record_unapplied` plus a FRESH executor makes the RunPython actually run: the
  test database is already fully migrated when the test begins, so `migrate` would
  compute an empty plan, and the executor's loader caches `applied_migrations` at
  build time — unapplying on the same executor still leaves the migration applied
  in the cached set. A second executor re-reads the recorder table and replays the
  operation under test against the real schema without re-creating a table.
"""

from datetime import date
from importlib import import_module
from typing import Any

import pytest
from django.apps.registry import Apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

APP = "clients"
BACKFILL = "0013_backfill_engagement_taxprofile"
# The schema state the seeds are built against is each app's LEAF, not the
# migration's own number: the rows are seeded through the historical models the
# state projects, and a state that predates a later NOT NULL column (tenants'
# `features`, clients' own `tax_profile_revision`) sends NULL into the live
# column the live schema requires — the seed state must match the schema it
# writes to, not merely the migration under test.
SCHEMA = "0014_clientcompany_tax_profile_revision"
TENANTS_SCHEMA = "0007_tenant_features"


def _create_tenant(apps: Apps, *, name: str, slug: str) -> Any:  # noqa: ANN401
    return apps.get_model("tenants", "Tenant").objects.create(name=name, slug=slug)


def _create_client(apps: Apps, tenant: Any, **overrides: Any) -> Any:  # noqa: ANN401
    fields: dict[str, Any] = {
        "tenant": tenant,
        "legal_name": "Cliente Legado",
        "cnpj": "12ABC34501DE35",
    }
    fields.update(overrides)
    return apps.get_model("clients", "ClientCompany").objects.create(**fields)


def _run_backfill_through_executor(executor: MigrationExecutor) -> Apps:
    """Replay exactly the migration under test and return the post state."""
    executor.recorder.record_unapplied(APP, BACKFILL)
    fresh = MigrationExecutor(connection)
    fresh.migrate([(APP, BACKFILL)])
    return fresh.loader.project_state((APP, BACKFILL)).apps


def _restore_schema(executor: MigrationExecutor) -> None:
    """Land every migration again so a failure cannot poison the next test."""
    executor.migrate(executor.loader.graph.leaf_nodes())
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE app_runtime")


def test_every_client_gains_one_profile_and_one_engagement() -> None:
    executor = MigrationExecutor(connection)
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        apps = executor.loader.project_state(
            [(APP, SCHEMA), ("tenants", TENANTS_SCHEMA)]
        ).apps

        alpha = _create_tenant(apps, name="Firma A", slug="firma-a")
        beta = _create_tenant(apps, name="Firma B", slug="firma-b")

        # Three clients on alpha exercising the two mapping arms, one on beta to
        # prove the backfill honours the tenant boundary rather than running global.
        mei_opened = _create_client(
            apps,
            alpha,
            legal_name="MEI com abertura",
            is_mei=True,
            mei_category="caminhoneiro",
            opened_on=date(2019, 4, 10),
        )
        mei_unopened = _create_client(
            apps,
            alpha,
            legal_name="MEI sem abertura",
            cnpj="33XYZ98765QR00",
            is_mei=True,
            mei_category="common",
        )
        not_mei = _create_client(
            apps,
            alpha,
            legal_name="Nao MEI",
            cnpj="99QQQ11122XX00",
            is_mei=False,
        )
        beta_client = _create_client(apps, beta, is_mei=True, mei_category="common")

        # An assignment proves the engagement inherits the portfolio's responsible.
        user = apps.get_model("accounts", "User").objects.create(
            email="responsavel@firma-a.test",
        )
        apps.get_model("clients", "ClientAssignment").objects.create(
            tenant_id=alpha.id, client_id=mei_opened.id, user_id=user.id
        )

        after = _run_backfill_through_executor(executor)
        profiles = after.get_model("clients", "TaxProfile")
        engagements = after.get_model("clients", "Engagement")

        alpha_profiles = {
            row.client_id: row for row in profiles.objects.filter(tenant_id=alpha.id)
        }
        beta_profiles = {
            row.client_id: row for row in profiles.objects.filter(tenant_id=beta.id)
        }
        alpha_engagements = {
            row.client_id: row for row in engagements.objects.filter(tenant_id=alpha.id)
        }

        # Then every client got exactly one of each, marked with the provenance
        # stamp that keeps a migrated guess distinguishable from a person's answer.
        assert len(alpha_profiles) == 3
        assert len(beta_profiles) == 1
        assert len(alpha_engagements) == 3
        assert all(
            row.provenance == "backfill_v1"
            for row in (
                *alpha_profiles.values(),
                *alpha_engagements.values(),
                *beta_profiles.values(),
            )
        )

        # The mapping: is_mei is the regime AND the pack, carried at the same date.
        opened = alpha_profiles[mei_opened.id]
        assert opened.regime == "mei"
        assert opened.mei_category == "caminhoneiro"
        assert opened.valid_from == date(2019, 4, 10)
        assert opened.valid_to is None
        assert opened.confidence == "asserted_by_legacy_flag"
        assert alpha_engagements[mei_opened.id].service_kind == "mei_mensal"
        assert alpha_engagements[mei_opened.id].responsible_id == user.id

        # An MEI with no recorded opening date is valid from its registry entry —
        # history before valid_from is never inferred.
        unopened = alpha_profiles[mei_unopened.id]
        assert unopened.regime == "mei"
        assert unopened.valid_from == mei_unopened.created_at.date()

        # The flag's absence maps to unknown, not to a regime somebody invented.
        unmappable = alpha_profiles[not_mei.id]
        assert unmappable.regime == "unknown"
        assert unmappable.mei_category is None
        assert alpha_engagements[not_mei.id].service_kind == "documentos_mensal"

        # The other firm's client was backfilled under ITS tenant, not alpha's.
        assert beta_profiles[beta_client.id].tenant_id == beta.id
        assert beta_profiles[beta_client.id].regime == "mei"
    finally:
        _restore_schema(executor)


def test_the_backfill_is_idempotent() -> None:
    executor = MigrationExecutor(connection)
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        apps = executor.loader.project_state(
            [(APP, SCHEMA), ("tenants", TENANTS_SCHEMA)]
        ).apps
        tenant = _create_tenant(apps, name="Idempotente", slug="idempotente")
        _create_client(apps, tenant, is_mei=False)

        after = _run_backfill_through_executor(executor)

        # Re-running the function itself — not just replaying the applied
        # migration, which would no-op for the wrong reason — must create nothing.
        migration_module = import_module(f"apps.clients.migrations.{BACKFILL}")
        profiles = after.get_model("clients", "TaxProfile")
        engagements = after.get_model("clients", "Engagement")
        before_profiles = profiles.objects.count()
        before_engagements = engagements.objects.count()
        assert before_profiles == before_engagements == 1
        migration_module.backfill(after, None)
        assert profiles.objects.count() == before_profiles
        assert engagements.objects.count() == before_engagements
    finally:
        _restore_schema(executor)
