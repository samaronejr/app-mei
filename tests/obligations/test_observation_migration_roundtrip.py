"""Reversing observations must leave the shared audit trigger function intact."""

from datetime import date
from importlib import import_module

import pytest
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.audit.services import record_event
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Obligation, ObligationStatus, SourceObservation
from apps.tenants.models import Tenant

pytestmark = pytest.mark.django_db(transaction=True)


def test_observations_reverse_and_reapply_without_weakening_audit() -> None:
    tenant = Tenant.objects.create(name="Roundtrip", slug="roundtrip")
    actor = User.objects.create_user(email="roundtrip@example.test")
    with tenant_context(tenant.id):
        client = ClientCompany.objects.create(
            tenant=tenant, legal_name="Roundtrip", cnpj="12ABC34501DE35"
        )
        obligation = Obligation.objects.create(
            tenant=tenant,
            client=client,
            obligation_type_id="DAS",
            competence_month=date(2026, 9, 1),
            nominal_due_date=date(2026, 10, 20),
            resolved_due_date=date(2026, 10, 20),
            status=ObligationStatus.PAID,
        )
        record_event(
            action=AuditAction.TAX_PROFILE_CORRECTED, tenant_id=tenant.id, actor=actor
        )
        event = Event.objects.get()
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        executor.migrate([("obligations", "0017_schedulerheartbeat_free_disk_kib")])
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('obligations_sourceobservation')")
            assert cursor.fetchone() == (None,)
            cursor.execute(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgfoid = 'audit_reject_mutation()'::regprocedure ORDER BY tgname"
            )
            assert cursor.fetchall() == [
                ("audit_event_append_only",),
                ("audit_platformevent_append_only",),
                ("cycles_cycleevent_append_only",),
                ("delivery_deliveryattempt_append_only",),
                ("delivery_deliveryeffect_append_only",),
                # T22's private usage ledger is supersede-only (correction_of),
                # so it carries the same append-only trigger; reversing the
                # observations table must leave it installed as well.
                ("tenants_usagerecord_append_only",),
            ]
        # As table owner, grants cannot mask a missing mutation trigger.
        with (
            tenant_context(tenant.id),
            pytest.raises(DatabaseError, match="append-only"),
            transaction.atomic(),
        ):
            Event.objects.filter(pk=event.pk).update(metadata={"changed": True})
        MigrationExecutor(connection).migrate(leaves)
        with tenant_context(tenant.id):
            observation = SourceObservation.objects.get(subject_ref=obligation)
            assert observation.source_kind == "legacy_status"
            assert observation.verification_status == "unknown"
            assert observation.observed_at == obligation.updated_at
            before = list(SourceObservation.objects.values())
        migration = import_module("apps.obligations.migrations.0018_sourceobservation")
        migration.backfill_paid_observations(
            MigrationExecutor(connection).loader.project_state(leaves).apps, None
        )
        with tenant_context(tenant.id):
            assert list(SourceObservation.objects.values()) == before
            with (
                pytest.raises(DatabaseError, match="append-only"),
                transaction.atomic(),
            ):
                SourceObservation.objects.filter(pk=observation.pk).update(
                    verification_status="verified"
                )
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute(
                "GRANT SELECT ON obligations_sourceobservation TO app_portal"
            )
            cursor.execute("SET ROLE app_runtime")
