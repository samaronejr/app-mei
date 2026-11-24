"""The additive storage head can read exact revision evidence without projecting it."""

from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.core.retention import RetentionClass
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, ExtractedField
from apps.cycles.extraction.v2 import extract
from apps.cycles.scanning import FakeScanner, scan_intake
from apps.obligations import fiscal_ingest
from apps.obligations.iss_retention_evidence_v1 import (
    ISSDecision,
    agreed_iss_code,
    revision_iss_originals,
    verify_iss_original,
)
from apps.obligations.models import AcquisitionOrigin, FiscalDocument
from apps.obligations.system_ingest import ingest_system_document
from tests.integrations.test_migration_rollback import _as_migrator
from tests.obligations import test_fiscal_documents
from tests.obligations.fiscal_fixtures import KEY, nfse
from tests.obligations.test_fiscal_documents import document, ingest, revision_of
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
setup = test_fiscal_documents.setup
BEFORE = ("obligations", "0029_fiscal_register")
ADD_PARTY = ("obligations", "0030_iss_retention_party")


def test_iss_retention_repair_old_writer_can_insert_after_migration(
    setup: PortalSetup,
) -> None:
    historical = MigrationExecutor(connection).loader.project_state(
        [("obligations", "0029_fiscal_register")]
    )
    old_writer = historical.apps.get_model("obligations", "FiscalDocument")
    with tenant_context(setup.graph.tenant.pk), transaction.atomic():
        old = old_writer._base_manager.create(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            environment="fake",
            family="nfse_nacional",
            key_namespace="nacional",
            document_key=f"NFS{KEY}",
            client_role="tomador",
            source="captura",
            provider="focus",
        )
        assert (
            FiscalDocument.objects.get(pk=old.pk).iss_retention_party == "desconhecido"
        )


def _legacy_register_bytes(fiscal_id: UUID) -> tuple[object, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT (to_jsonb(f) - 'iss_retention_party')::text,
                   row_to_json(r)::text, row_to_json(a)::text, row_to_json(d)::text
            FROM obligations_fiscaldocument f
            JOIN obligations_fiscalrevision r ON r.id = f.current_revision_id
            JOIN obligations_revisionartifact a ON a.revision_id = r.id
            JOIN obligations_document d ON d.id = a.document_id
            WHERE f.id = %s
            """,
            [fiscal_id],
        )
        rows = cursor.fetchall()
    assert len(rows) == 1
    return tuple(rows[0])


def test_iss_party_round_trip_preserves_populated_0029_register(
    setup: PortalSetup,
) -> None:
    tenant_id, client_id = setup.graph.tenant.pk, setup.graph.client.pk
    payload = nfse()
    receipt_ref = "code-2-receipt"
    with _as_migrator():
        executor = MigrationExecutor(connection)
        executor.migrate([BEFORE])
        historical = executor.loader.project_state([BEFORE]).apps
        original = ingest_system_document(
            tenant_id=tenant_id,
            client_id=client_id,
            origin=AcquisitionOrigin.PROVIDER_CAPTURE,
            receipt_ref=receipt_ref,
            payload=payload,
            filename="documento-fiscal.xml",
            content_type="application/xml",
            retention_class=RetentionClass.FISCAL_EVIDENCE,
        )
        with tenant_context(tenant_id):
            intake_id = DocumentIntake.objects.get(document=original).pk
        scan_intake(tenant_id, intake_id, scanner=FakeScanner(), now=timezone.now())
        scope = {"tenant_id": tenant_id, "client_id": client_id}
        with tenant_context(tenant_id):
            intake = DocumentIntake.objects.get(pk=intake_id)
            fiscal_ingest._classify(
                original, intake, extract(intake, payload), "confirmed"
            )
            old = historical.get_model(
                "obligations", "FiscalDocument"
            )._base_manager.create(
                **scope,
                environment="fake",
                family="nfse_nacional",
                key_namespace="nacional",
                document_key=f"NFS{KEY}",
                client_role="tomador",
                source="captura",
                provider="focus",
                fiscal_state="autorizada",
                summary_only=False,
                role_verification="confirmed",
                iss_stated=Decimal("12.34"),
                iss_withheld_flag=True,
                iss_withheld_amount=Decimal("12.34"),
            )
            revision = historical.get_model(
                "obligations", "FiscalRevision"
            )._base_manager.create(
                **scope,
                document_id=old.pk,
                source_namespace="focus:nfsen_recebidas",
                provider_version="1",
                semantic_digest=sha256(b"synthetic semantics").hexdigest(),
                receipt_ref=receipt_ref,
                fiscal_state="autorizada",
            )
            historical.get_model(
                "obligations", "RevisionArtifact"
            )._base_manager.create(
                **scope,
                revision_id=revision.pk,
                document_id=original.pk,
                role="xml_original",
            )
            old.current_revision_id = revision.pk
            old.save(update_fields=["current_revision"])
        token = ExtractedField._base_manager.get(
            document=original, name="iss_withheld_flag"
        )
        assert token.value == "2"
        before = _legacy_register_bytes(old.pk)
        executor = MigrationExecutor(connection)
        assert [
            (m.app_label, m.name, backwards)
            for m, backwards in executor.migration_plan([ADD_PARTY])
        ] == [(*ADD_PARTY, False)]
        executor.migrate([ADD_PARTY])
        with tenant_context(tenant_id):
            upgraded = FiscalDocument.objects.get(pk=old.pk)
        assert upgraded.iss_retention_party == "desconhecido"
        assert (upgraded.iss_withheld_flag, upgraded.iss_withheld_amount) == (
            True,
            Decimal("12.34"),
        )
        assert _legacy_register_bytes(old.pk) == before
        executor = MigrationExecutor(connection)
        assert [
            (m.app_label, m.name, backwards)
            for m, backwards in executor.migration_plan([BEFORE])
        ] == [(*ADD_PARTY, True)]
        executor.migrate([BEFORE])
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, "obligations_fiscaldocument"
            )
        assert "iss_retention_party" not in {column.name for column in columns}
        assert _legacy_register_bytes(old.pk) == before
        with default_storage.open(original.storage_key, "rb") as retained:
            assert retained.read() == payload


def test_iss_evidence_resolves_only_the_current_scoped_original(
    setup: PortalSetup,
) -> None:
    tenant, client = setup.graph.tenant.pk, setup.graph.client.pk
    original = nfse()
    options = {"client_role": "tomador", "source_namespace": "focus:nfse_recebidas"}
    ingest(
        setup,
        payload=original.replace(b">2</tpRetISSQN>", b">3</tpRetISSQN>"),
        version=1,
        **options,
    )
    stale = revision_of(document(setup))
    ingest(setup, payload=original, version=2, **options)
    fiscal = document(setup)
    revision = revision_of(fiscal)
    scope = (tenant, client, revision.pk)
    with tenant_context(tenant):
        assert stale.pk != revision.pk
        current = revision_iss_originals(connection, scope)
        foreign = revision_iss_originals(connection, (tenant, uuid4(), revision.pk))
        assert agreed_iss_code(foreign, verify_iss_original) == ISSDecision()
        assert len(current) == 1
        document_id = current[0].document_id
        assert document_id is not None
        ExtractedField.objects.filter(document_id=document_id).update(
            redacted_at=revision.observed_at
        )
        redacted = revision_iss_originals(connection, scope)
    assert agreed_iss_code(current, verify_iss_original) == ISSDecision("2")
    assert agreed_iss_code(redacted, verify_iss_original) == ISSDecision()
    assert fiscal.iss_withheld_flag is True
    assert fiscal.iss_withheld_amount == Decimal("12.34")
