"""Populated reverse/reapply and early forward-repair refusals for AXRM-012."""

from datetime import date
from pathlib import Path

import pytest
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.obligations.models import Document, Obligation
from apps.tenants.models import Tenant
from tests.obligations.ingest_fixtures import PDF, ingest_setup
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
PREVIOUS = ("obligations", "0022_schedulerheartbeat_job_outcome")
FOLDED_POLICIES = tuple(
    f"{table}_portal_client_isolation"
    for table in (
        "obligations_document",
        "obligations_obligation",
        "cycles_documentintake",
        "cycles_evidencerequest",
        "cycles_evidencelink",
        "obligations_sourceobservation",
    )
)


def _folded_policies() -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            # pg_policies pretty-prints the predicate across lines (CASE / WHEN).
            "SELECT count(*) FROM pg_policies WHERE policyname = ANY(%s) AND qual ~ %s",
            [list(FOLDED_POLICIES), r"CASE\s+WHEN"],
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def test_populated_two_tenant_legacy_rows_reverse_and_reapply_without_changes() -> None:
    # Given legacy documents and obligations in two firms, with distinct upload origins.
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    actor = User.objects.create_user(email="roundtrip-provenance@example.test")
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        executor.migrate([PREVIOUS])
        old_apps = MigrationExecutor(connection).loader.project_state([PREVIOUS]).apps
        old_document = old_apps.get_model("obligations", "Document")
        old_obligation = old_apps.get_model("obligations", "Obligation")
        document_ids = []
        obligation_ids = []
        expected_origins = {}
        for number in (1, 2):
            tenant = Tenant.objects.create(
                name=f"Roundtrip {number}", slug=f"roundtrip-{number}"
            )
            with tenant_context(tenant.pk):
                client = ClientCompany.objects.create(
                    tenant=tenant, legal_name="Synthetic", cnpj="11222333000181"
                )
            for portal in (True, False):
                document = old_document.objects.create(
                    tenant_id=tenant.pk,
                    client_id=client.pk,
                    sha256=("a" if portal else "b") * 64,
                    original_filename=f"legacy-{portal}.pdf",
                    content_type="application/pdf",
                    byte_size=10,
                    uploaded_by_id=actor.pk,
                    client_upload=portal,
                )
                document_ids.append(document.pk)
                expected_origins[document.pk] = (
                    "upload_portal" if portal else "upload_equipe"
                )
            obligation_ids.append(
                old_obligation.objects.create(
                    tenant_id=tenant.pk,
                    client_id=client.pk,
                    obligation_type_id="DAS",
                    competence_month=date(2026, 9, 1),
                    nominal_due_date=date(2026, 10, 20),
                    resolved_due_date=date(2026, 10, 20),
                ).pk
            )

        # When the new migration is applied, reversed and reapplied on these rows.
        for _ in range(2):
            MigrationExecutor(connection).migrate(leaves)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, portal_visible, acquisition_origin, "
                    "acquisition_receipt_ref "
                    "FROM obligations_document ORDER BY id"
                )
                rows = cursor.fetchall()
                assert {row[0] for row in rows} == set(document_ids)
                assert all(row[1] is True and row[3] == "" for row in rows)
                assert {row[0]: row[2] for row in rows} == expected_origins
                cursor.execute("SELECT id, portal_visible FROM obligations_obligation")
                obligations = cursor.fetchall()
                assert {row[0] for row in obligations} == set(obligation_ids)
                assert all(row[1] is True for row in obligations)
            assert _folded_policies() == len(FOLDED_POLICIES)
            MigrationExecutor(connection).migrate([PREVIOUS])
            with connection.cursor() as cursor:
                cursor.execute("SELECT id FROM obligations_document")
                assert {row[0] for row in cursor.fetchall()} == set(document_ids)
                cursor.execute(
                    "SELECT pg_get_functiondef("
                    "'document_portal_client_upload()'::regprocedure)"
                )
                assert "portal_visible" not in cursor.fetchone()[0]
            # The reverse restores each pre-existing client-only predicate.
            assert _folded_policies() == 0
        # Then every original survives and the final schema is restored below.
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    return ingest_setup(settings, tmp_path)


@pytest.mark.parametrize(
    "reason",
    [
        "provider",
        "visible_provider",
        "hash_pair",
        "private_staff",
        "private_obligation",
    ],
)
def test_reverse_refuses_before_undoing_any_isolation_control(
    setup: PortalSetup, reason: str
) -> None:
    from apps.obligations.system_ingest import ingest_system_document  # noqa: PLC0415

    # Given retained new data for which reversal would expose or misattribute a row.
    if reason in {"provider", "visible_provider"}:
        document = ingest_system_document(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            origin="provedor_federal",
            receipt_ref="roundtrip-call",
            payload=PDF,
            filename="report.pdf",
            content_type="application/pdf",
            retention_class="fiscal_evidence",
        )
        if reason == "visible_provider":
            with tenant_context(setup.graph.tenant.pk):
                Document.objects.filter(pk=document.pk).update(portal_visible=True)
    else:
        with tenant_context(setup.graph.tenant.pk):
            if reason == "private_obligation":
                Obligation.objects.create(
                    tenant=setup.graph.tenant,
                    client=setup.graph.client,
                    obligation_type_id="DAS",
                    competence_month=date(2030, 1, 1),
                    nominal_due_date=date(2030, 2, 20),
                    resolved_due_date=date(2030, 2, 20),
                    portal_visible=False,
                )
            else:
                for visible in (False, True) if reason == "hash_pair" else (False,):
                    Document.objects.create(
                        tenant=setup.graph.tenant,
                        client=setup.graph.client,
                        uploaded_by=setup.graph.actor,
                        client_upload=False,
                        portal_visible=visible,
                        acquisition_origin="upload_equipe",
                        sha256="a" * 64,
                        byte_size=10,
                        content_type="application/pdf",
                        original_filename="private.pdf",
                    )
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        # When the real migration executor attempts a reverse.
        with pytest.raises(DatabaseError, match=r"AXRM-012.*forward-repair"):
            executor.migrate([PREVIOUS])
        # Then it refused early: the visibility policy and nullable actor still exist.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT qual FROM pg_policies WHERE tablename = 'obligations_document' "
                "AND policyname = 'obligations_document_portal_client_isolation'"
            )
            assert "portal_visible" in cursor.fetchone()[0]
    finally:
        MigrationExecutor(connection).migrate(leaves)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
