"""AXRM-020 acceptance through the real vault, scanner and PostgreSQL policies."""

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db import DatabaseError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from pytest_django.fixtures import SettingsWrapper

from apps.audit.models import Event
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    ExtractedField,
    IntakeState,
)
from apps.cycles.records import Exception as CycleException
from apps.cycles.scanning import FakeScanner
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.obligations.fiscal_ingest import ingest_fiscal_document
from apps.obligations.models import (
    Document,
    FiscalDocument,
    FiscalRevision,
    RevisionArtifact,
)
from apps.tenants.models import Tenant
from tests.obligations.fiscal_fixtures import (
    CLIENT,
    COUNTERPART,
    KEY,
    nfe,
    nfse,
    summary,
)
from tests.obligations.ingest_fixtures import ingest_setup, portal_cursor
from tests.portal.request_fixtures import PortalSetup

if TYPE_CHECKING:
    from apps.accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> PortalSetup:
    result = ingest_setup(settings, tmp_path)
    result.graph.tenant.features = ["cycles_r1", "scan_required", "document_capture"]
    result.graph.tenant.save(update_fields=["features"])
    settings.CLAMAV_ENABLED = True
    return result


@pytest.fixture
def binding(setup: PortalSetup) -> ClientProviderLink:
    with tenant_context(setup.graph.tenant.pk):
        deployment_ref = "axrm020/fake"
        connection = ProviderConnection.objects.create(
            tenant=setup.graph.tenant,
            provider="focus",
            environment="fake",
            secret_ref=deployment_ref,
            status="ativa",
        )
        return ClientProviderLink.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            connection=connection,
            provider_company_ref="700020",
            certificate_cnpj=CLIENT,
            status="ativa",
            credential_state="ativa",
        )


def ingest(setup: PortalSetup, **options: object) -> object:
    version = cast("int", options.get("version", 12))
    source = cast("str", options.get("source", "captura"))
    binding = cast("ClientProviderLink | None", options.get("binding"))
    scanner = cast("FakeScanner", options.get("scanner", FakeScanner()))
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=scanner,
    ):
        return ingest_fiscal_document(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            source=source,
            provider=cast("str", options.get("provider", "focus")),
            provider_version=str(version),
            source_namespace=cast(
                "str", options.get("source_namespace", "focus:nfe_recebidas")
            ),
            environment="fake",
            client_role=cast("str", options.get("client_role", "destinatario")),
            receipt_ref=f"fake-call-{version}",
            payload_bytes=cast("bytes | None", options.get("payload")),
            summary=cast("dict[str, str] | None", options.get("head_summary")),
            actor=cast(
                "User | None",
                options.get(
                    "actor",
                    setup.portal_user
                    if source == "upload_cliente"
                    else setup.graph.actor
                    if source == "importacao_equipe"
                    else None,
                ),
            ),
            capture_binding_id=binding.pk if binding else None,
            fiscal_state=cast("str | None", options.get("fiscal_state")),
            replacement_key=cast("str", options.get("replacement_key", "")),
            source_effective_at=cast(
                "datetime | None", options.get("source_effective_at")
            ),
        )


def document(setup: PortalSetup) -> FiscalDocument:
    with tenant_context(setup.graph.tenant.pk):
        return FiscalDocument.objects.select_related("current_revision").get()


def revision_of(fiscal: FiscalDocument) -> FiscalRevision:
    revision = fiscal.current_revision
    assert revision is not None
    return revision


def test_bound_summary_v10_then_full_v12_has_one_identity_and_confirmed_role(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    ingest(setup, version=10, head_summary=summary(), binding=binding)
    provisional = document(setup)
    assert provisional.summary_only
    assert provisional.role_verification == "unconfirmed"
    assert revision_of(provisional).provenance == {
        "role_basis": "capture_binding",
        "link_id": str(binding.pk),
        "provider_company_ref": "700020",
        "client_cnpj": CLIENT,
    }
    with tenant_context(setup.graph.tenant.pk):
        assert not CycleException.objects.filter(
            reason_code="documento_fiscal_divergente"
        ).exists()
    ingest(setup, payload=nfe(), binding=binding)
    current = document(setup)
    assert current.pk == provisional.pk
    assert (current.document_key, current.summary_only, current.role_verification) == (
        KEY,
        False,
        "confirmed",
    )
    assert revision_of(current).provider_version == "12"
    assert current.fiscal_state == "autorizada"
    assert current.version == provisional.version + 1
    assert current.environment == "fake"
    assert current.family == "nfe"
    assert current.key_namespace == "sefaz"
    assert current.client_role == "destinatario"
    assert current.counterpart_id == COUNTERPART
    assert current.counterpart_name == "Synthetic issuer"
    assert current.source == "captura"
    assert current.provider == "focus"
    assert str(current.issue_date) == "2026-09-15"
    assert str(current.total) == "1500.25"
    assert revision_of(current).source_namespace == "focus:nfe_recebidas"
    assert revision_of(current).receipt_ref == "fake-call-12"
    assert revision_of(current).provenance == {"role_basis": "xml"}
    assert revision_of(current).semantic_digest == (
        "6c0101851118b3dad359b45678a54c5c2a06b3a49c92768d7a5cdb0cabd4940a"
    )
    with tenant_context(setup.graph.tenant.pk):
        assert FiscalRevision.objects.count() == 2
        assert set(RevisionArtifact.objects.values_list("role", flat=True)) == {
            "resumo",
            "xml_original",
        }
        assert set(
            ExtractedField.objects.values_list("extractor_version", flat=True)
        ) == {"2"}
        assert not EvidenceLink.objects.exists()
        for artifact in RevisionArtifact.objects.select_related("document", "revision"):
            raw = artifact.document
            intake = DocumentIntake.objects.get(document=raw)
            assert intake.state == IntakeState.CLASSIFIED
            assert intake.version == 3
            assert raw.uploaded_by_id is None
            assert raw.acquisition_origin == "captura_provedor"
            assert raw.acquisition_receipt_ref == (
                f"fake-call-{artifact.revision.provider_version}"
            )
            assert artifact.role == (
                "resumo"
                if artifact.revision.provider_version == "10"
                else "xml_original"
            )
            assert raw.original_filename == "documento-fiscal.xml"
            assert raw.content_type == "application/xml"
            assert raw.retention_class == "fiscal_evidence"
            assert (raw.portal_visible, raw.client_upload) == (False, False)
            assert default_storage.exists(raw.storage_key)
        assert Event.objects.filter(action="fiscal.head_changed").count() == 2
        assert Event.objects.filter(action="fiscal.revision_registered").count() == 2
        assert not CycleException.objects.filter(
            reason_code="documento_fiscal_divergente"
        ).exists()


def test_v12_then_v11_records_stale_revision_without_regressing_head(
    setup: PortalSetup,
) -> None:
    ingest(setup, payload=nfe(state="101"))
    original = document(setup)
    ingest(setup, version=11, payload=nfe(total="900.00"))
    current = document(setup)
    assert current.current_revision_id == original.current_revision_id
    assert current.fiscal_state == "cancelada"
    with tenant_context(setup.graph.tenant.pk):
        stale = FiscalRevision.objects.get(provider_version="11")
        assert stale.pk != current.current_revision_id
        assert FiscalRevision.objects.count() == 2


def test_same_version_same_semantics_reuses_revision_and_bytes(
    setup: PortalSetup,
) -> None:
    ingest(setup, payload=nfe())
    first = document(setup)
    with tenant_context(setup.graph.tenant.pk):
        intake_version = DocumentIntake.objects.get().version
    ingest(setup, payload=nfe())
    current = document(setup)
    assert current.current_revision_id == first.current_revision_id
    assert current.version == first.version
    with tenant_context(setup.graph.tenant.pk):
        assert FiscalRevision.objects.count() == 1
        assert RevisionArtifact.objects.count() == 1
        assert DocumentIntake.objects.get().version == intake_version
        assert Event.objects.filter(action="fiscal.head_changed").count() == 1
        assert Event.objects.filter(action="fiscal.revision_registered").count() == 1


def test_same_version_different_semantics_opens_consistency_incident(
    setup: PortalSetup,
) -> None:
    ingest(setup, payload=nfe())
    original = document(setup)
    ingest(setup, payload=nfe(total="1200.00"))
    current = document(setup)
    assert current.current_revision_id == original.current_revision_id
    assert current.fiscal_state == "incerta"
    assert current.version == original.version + 1
    with tenant_context(setup.graph.tenant.pk):
        assert CycleException.objects.filter(
            reason_code="incidente_consistencia_fiscal"
        ).exists()
        assert FiscalRevision.objects.count() == 1
        assert Event.objects.filter(action="fiscal.consistency_incident").count() == 1
        event = Event.objects.get(action="fiscal.consistency_incident")
        conflicting = Document.objects.exclude(
            pk__in=RevisionArtifact.objects.values("document_id")
        ).get()
        assert (event.object_type, event.object_id) == (
            "FiscalDocument",
            str(current.pk),
        )
        assert event.metadata == {"original_document_id": str(conflicting.pk)}


def test_newer_snapshot_cannot_erase_established_cancellation(
    setup: PortalSetup,
) -> None:
    ingest(setup, version=10, payload=nfe(state="101"))
    cancelled = document(setup)
    ingest(setup, payload=nfe())
    current = document(setup)
    assert current.current_revision_id == cancelled.current_revision_id
    assert current.fiscal_state == "incerta"
    with tenant_context(setup.graph.tenant.pk):
        assert FiscalRevision.objects.count() == 2


def test_summary_then_wrong_recipient_full_xml_becomes_divergent_with_exception(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    ingest(setup, version=10, head_summary=summary(), binding=binding)
    ingest(setup, payload=nfe(recipient=COUNTERPART))
    current = document(setup)
    assert current.role_verification == "divergent"
    with tenant_context(setup.graph.tenant.pk):
        assert CycleException.objects.filter(
            reason_code="documento_fiscal_divergente"
        ).exists()
        assert DocumentIntake.objects.filter(state=IntakeState.DIVERGENT).exists()
        assert (
            DocumentIntake.objects.get(
                document=RevisionArtifact.objects.get(
                    revision_id=current.current_revision_id
                ).document
            ).reason
            == "documento_fiscal_divergente"
        )
        assert not EvidenceLink.objects.exists()


@pytest.mark.parametrize("source", ["captura", "importacao_equipe", "upload_cliente"])
def test_recipientless_summary_without_binding_is_refused(
    setup: PortalSetup, source: str
) -> None:
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), source=source)
    with tenant_context(setup.graph.tenant.pk):
        assert not FiscalDocument.objects.exists()


def test_capture_link_company_cnpj_mismatch_is_refused(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        binding.certificate_cnpj = COUNTERPART
        binding.save(update_fields=["certificate_cnpj"])
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), binding=binding)
    with tenant_context(setup.graph.tenant.pk):
        assert not FiscalDocument.objects.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "configuracao_pendente"),
        ("status", "revogada"),
        ("credential_state", "divergente"),
        ("credential_state", "vencida"),
        ("disablement_state", "pausada_localmente"),
    ],
)
def test_inactive_disabled_or_divergent_binding_is_refused(
    setup: PortalSetup, binding: ClientProviderLink, field: str, value: str
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        setattr(binding, field, value)
        binding.save(update_fields=[field])
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), binding=binding)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "serpro"),
        ("environment", "homologacao"),
        ("status", "pausada"),
    ],
)
def test_binding_provider_and_environment_must_match_ingest(
    setup: PortalSetup, binding: ClientProviderLink, field: str, value: str
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        setattr(binding.connection, field, value)
        binding.connection.save(update_fields=[field])
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), binding=binding)


def test_explicit_missing_binding_does_not_fall_back_to_another_active_link(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    binding.pk = uuid4()
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), binding=binding)
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()
        assert not FiscalDocument.objects.exists()


def test_expiring_valid_binding_keeps_opaque_company_reference(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        binding.credential_state = "vencendo"
        binding.save(update_fields=["credential_state"])
    ingest(setup, head_summary=summary(), binding=binding)
    assert revision_of(document(setup)).provenance["provider_company_ref"] == "700020"


def test_binding_client_must_match_ingest(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        binding.client = setup.other_client
        binding.save(update_fields=["client"])
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), binding=binding)


def test_unset_certificate_cnpj_does_not_change_opaque_company_reference(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        binding.certificate_cnpj = ""
        binding.save(update_fields=["certificate_cnpj"])
    ingest(setup, head_summary=summary(), binding=binding)
    assert revision_of(document(setup)).provenance["provider_company_ref"] == "700020"


def test_staff_import_is_invisible_to_actual_portal_role(setup: PortalSetup) -> None:
    ingest(setup, payload=nfe(), source="importacao_equipe")
    current = document(setup)
    with tenant_context(setup.graph.tenant.pk):
        raw = RevisionArtifact.objects.select_related("document").get().document
        assert raw.uploaded_by_id == setup.graph.actor.pk
        assert raw.acquisition_origin == "upload_equipe"
        assert not raw.portal_visible
    with portal_cursor(setup) as cursor:
        cursor.execute("SELECT id FROM obligations_document WHERE id = %s", [raw.pk])
        assert cursor.fetchall() == []
        for table in (
            "obligations_fiscaldocument",
            "obligations_fiscalrevision",
            "obligations_revisionartifact",
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(f"SELECT id FROM {table}")  # noqa: S608 - fixed allowlist
    assert current.client_id == setup.graph.client.pk


def test_reverse_migration_refuses_while_fiscal_rows_exist(setup: PortalSetup) -> None:
    ingest(setup, payload=nfe())
    module = import_module("apps.obligations.migrations.0029_fiscal_register")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE app_test")
        cursor.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cursor.fetchone() == (True,)
        with pytest.raises(IrreversibleError, match="forward repair"):
            module.refuse_reverse_with_rows(apps, None)


def test_revisions_and_artifacts_cannot_be_updated_or_deleted(
    setup: PortalSetup,
) -> None:
    ingest(setup, payload=nfe())
    with tenant_context(setup.graph.tenant.pk), connection.cursor() as cursor:
        for table in ("obligations_fiscalrevision", "obligations_revisionartifact"):
            for query in (
                f"UPDATE {table} SET version = version + 1",  # noqa: S608
                f"DELETE FROM {table}",  # noqa: S608
            ):
                with pytest.raises(DatabaseError), transaction.atomic():
                    cursor.execute(query)


@pytest.mark.parametrize(
    "payload",
    [
        b" " * (5 * 1024 * 1024 + 1),
        (
            b'<?xml version="1.0"?><!DOCTYPE a ['
            b'<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>'
        ),
        (
            b'<?xml version="1.0"?><!DOCTYPE a ['
            b'<!ENTITY x "ha"><!ENTITY y "&x;&x;&x;">]><a>&y;</a>'
        ),
    ],
)
def test_unsafe_bytes_never_create_fiscal_identity(
    setup: PortalSetup, payload: bytes
) -> None:
    from apps.cycles.parsing import UnsafeDocument  # noqa: PLC0415

    with pytest.raises(UnsafeDocument):
        ingest(setup, payload=payload)
    with tenant_context(setup.graph.tenant.pk):
        assert not FiscalDocument.objects.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>',
        b'<!DOCTYPE a [<!ENTITY x "ha"><!ENTITY y "&x;&x;&x;">]><a>&y;</a>',
    ],
)
def test_doctype_without_xml_header_is_refused_by_the_intake_type_guard(
    setup: PortalSetup, payload: bytes
) -> None:
    from apps.cycles.intake import UploadRejected  # noqa: PLC0415

    with pytest.raises(UploadRejected):
        ingest(setup, payload=payload)
    with tenant_context(setup.graph.tenant.pk):
        assert DocumentIntake.objects.get().state == IntakeState.REJECTED_TYPE
        assert not FiscalDocument.objects.exists()


@pytest.mark.parametrize("total", ["NaN", "Infinity", "-1.00", "12.345", "bad"])
def test_invalid_fiscal_amount_never_updates_the_head(
    setup: PortalSetup, total: str
) -> None:
    with pytest.raises(ValidationError):
        ingest(setup, payload=nfe(total=total))
    with tenant_context(setup.graph.tenant.pk):
        assert not FiscalDocument.objects.exists()


def test_unassigned_staff_cannot_import_for_another_client(setup: PortalSetup) -> None:
    from django.http import Http404  # noqa: PLC0415

    with pytest.raises(Http404):
        ingest_fiscal_document(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            source="importacao_equipe",
            provider="focus",
            provider_version="1",
            source_namespace="manual",
            environment="fake",
            client_role="destinatario",
            receipt_ref="",
            payload_bytes=nfe(),
            summary=None,
            actor=setup.graph.staff,
        )
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()
        assert not FiscalDocument.objects.exists()


def test_head_cannot_reference_another_identity_of_the_same_client(
    setup: PortalSetup,
) -> None:
    ingest(setup, payload=nfe())
    first = document(setup)
    other_key = "35" + "0" * 42
    ingest(setup, payload=nfe().replace(KEY.encode(), other_key.encode()))
    with tenant_context(setup.graph.tenant.pk):
        other = FiscalDocument.objects.exclude(pk=first.pk).get()
        with pytest.raises(DatabaseError), transaction.atomic():
            FiscalDocument.objects.filter(pk=first.pk).update(
                current_revision_id=other.current_revision_id
            )


def test_capture_binding_from_another_tenant_is_refused(setup: PortalSetup) -> None:
    foreign = Tenant.objects.create(
        name="Synthetic foreign firm", slug="fiscal-foreign"
    )
    with tenant_context(foreign.pk):
        client = setup.graph.client.__class__.objects.create(
            tenant=foreign, legal_name="Synthetic foreign client", cnpj=CLIENT
        )
        connection = ProviderConnection.objects.create(
            tenant=foreign, provider="focus", environment="fake", status="ativa"
        )
        link = ClientProviderLink.objects.create(
            tenant=foreign,
            client=client,
            connection=connection,
            provider_company_ref="700020",
            status="ativa",
            credential_state="ativa",
        )
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), binding=link)
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()


def test_disabled_capture_refuses_before_vault_or_register_write(
    setup: PortalSetup,
) -> None:
    setup.graph.tenant.features = ["cycles_r1"]
    setup.graph.tenant.save(update_fields=["features"])
    with pytest.raises(PermissionDenied):
        ingest(setup, payload=nfe())
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()
        assert not FiscalDocument.objects.exists()


def test_scanner_outage_keeps_original_quarantined_and_resume_reuses_it(
    setup: PortalSetup,
) -> None:
    assert ingest(setup, payload=nfe(), scanner=FakeScanner(down=True)) is None
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get()
        assert intake.state == IntakeState.QUARANTINED
        assert intake.scan_attempts == 1
        assert not FiscalDocument.objects.exists()
        original_id = intake.document_id
    ingest(setup, payload=nfe())
    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.get().pk == original_id
        assert DocumentIntake.objects.get().state == IntakeState.CLASSIFIED
        assert FiscalRevision.objects.count() == 1


def test_unknown_layout_keeps_note_and_unconfirmed_role_for_review(
    setup: PortalSetup,
) -> None:
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        result = ingest_fiscal_document(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            source="captura",
            provider="focus",
            provider_version="12",
            source_namespace="focus:nfse_recebidas",
            environment="fake",
            client_role="tomador",
            receipt_ref="fake-call-12",
            payload_bytes=nfse(version="99.99"),
            summary=None,
            actor=None,
        )
    assert result is not None
    assert result.role_verification == "unconfirmed"
    with tenant_context(setup.graph.tenant.pk):
        note = ExtractedField.objects.get(name="layout_note")
        assert (note.value, note.extractor_version) == ("layout nao reconhecido", "2")
        assert DocumentIntake.objects.get().reason == "layout nao reconhecido"


def test_national_ingest_projects_only_stated_iss_values(setup: PortalSetup) -> None:
    from decimal import Decimal  # noqa: PLC0415

    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        result = ingest_fiscal_document(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            source="captura",
            provider="focus",
            provider_version="1",
            source_namespace="focus:nfse_recebidas",
            environment="fake",
            client_role="tomador",
            receipt_ref="fake-call-nfse",
            payload_bytes=nfse(),
            summary=None,
            actor=None,
        )
    assert result is not None
    assert result.role_verification == "confirmed"
    assert result.total == Decimal("800.00")
    assert result.iss_stated == Decimal("12.34")
    assert result.iss_withheld_flag is True
    assert result.iss_withheld_amount == Decimal("12.34")
    assert result.municipality_incidence == "3303302"


def test_client_upload_keeps_visible_portal_row_separate_from_capture(
    setup: PortalSetup,
) -> None:
    ingest(setup, payload=nfe())
    ingest(setup, payload=nfe(), source="upload_cliente", version=13)
    with tenant_context(setup.graph.tenant.pk):
        visible = Document.objects.get(portal_visible=True)
        assert visible.uploaded_by_id == setup.portal_user.pk
        assert visible.acquisition_origin == "upload_portal"
        assert visible.client_upload
        assert Document.objects.count() == 2
        assert not EvidenceLink.objects.exists()
    with portal_cursor(setup) as cursor:
        cursor.execute("SELECT id FROM obligations_document")
        assert cursor.fetchall() == [(visible.pk,)]


def test_stale_snapshot_keeps_extraction_and_revision_audit(setup: PortalSetup) -> None:
    ingest(setup, payload=nfe())
    ingest(setup, version=11, payload=nfe(total="1000.00"))
    with tenant_context(setup.graph.tenant.pk):
        stale = FiscalRevision.objects.get(provider_version="11")
        original = RevisionArtifact.objects.get(revision=stale).document
        assert ExtractedField.objects.filter(
            document=original, name="total", value="1000.00", extractor_version="2"
        ).exists()
        assert (
            DocumentIntake.objects.get(document=original).state
            == IntakeState.CLASSIFIED
        )
        assert (
            Event.objects.filter(
                action="fiscal.revision_registered", object_id=str(stale.pk)
            ).count()
            == 1
        )


def test_versions_are_ordered_within_their_source_namespace(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    ingest(setup, version=10, head_summary=summary(), binding=binding)
    with patch(
        "apps.obligations.fiscal_ingest.scanner_from_settings",
        return_value=FakeScanner(),
    ):
        imported = ingest_fiscal_document(
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            source="importacao_equipe",
            provider="",
            provider_version="1",
            source_namespace="staff:manual",
            environment="fake",
            client_role="destinatario",
            receipt_ref="",
            payload_bytes=nfe(),
            summary=None,
            actor=setup.graph.actor,
        )
    assert imported is not None
    assert imported.role_verification == "confirmed"
    assert revision_of(imported).source_namespace == "staff:manual"
    head = imported.current_revision_id
    ingest(setup, version=9, head_summary=summary(total="100.00"), binding=binding)
    current = document(setup)
    assert current.current_revision_id == head
    assert current.summary_only is False


def test_alphanumeric_client_binding_and_full_xml_attribution(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    from tests.obligations.fiscal_fixtures import ALPHANUMERIC  # noqa: PLC0415

    with tenant_context(setup.graph.tenant.pk):
        setup.graph.client.cnpj = ALPHANUMERIC
        setup.graph.client.save(update_fields=["cnpj"])
        binding.certificate_cnpj = ALPHANUMERIC
        binding.save(update_fields=["certificate_cnpj"])
    ingest(setup, version=10, head_summary=summary(), binding=binding)
    provisional = document(setup)
    assert revision_of(provisional).provenance["client_cnpj"] == ALPHANUMERIC
    ingest(setup, payload=nfe(recipient=ALPHANUMERIC))
    assert document(setup).role_verification == "confirmed"


def test_equal_versions_in_different_namespaces_are_distinct_revisions(
    setup: PortalSetup,
) -> None:
    ingest(setup, payload=nfe(), version=12)
    first = document(setup)
    ingest(
        setup,
        payload=nfe(total="1600.00"),
        version=12,
        source="importacao_equipe",
        source_namespace="staff:manual",
    )
    current = document(setup)
    assert current.pk == first.pk
    assert current.current_revision_id != first.current_revision_id
    assert current.fiscal_state == "autorizada"
    assert revision_of(current).source_namespace == "staff:manual"
    with tenant_context(setup.graph.tenant.pk):
        assert FiscalRevision.objects.count() == 2
        assert not CycleException.objects.filter(
            reason_code="incidente_consistencia_fiscal"
        ).exists()


def test_ingest_refuses_outer_transaction_before_any_original_is_stored(
    setup: PortalSetup,
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        with pytest.raises(RuntimeError, match="External I/O"):
            ingest(setup, payload=nfe())
        assert not Document.objects.exists()
        assert not FiscalDocument.objects.exists()


def test_five_mib_ingest_cap_refuses_before_storage(setup: PortalSetup) -> None:
    from apps.cycles.parsing import UnsafeDocument  # noqa: PLC0415

    raw = nfe()
    oversized = raw + b" " * (5 * 1024 * 1024 + 1 - len(raw))
    with pytest.raises(UnsafeDocument, match="too_large"):
        ingest(setup, payload=oversized)
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()
        assert not FiscalDocument.objects.exists()


@pytest.mark.parametrize("length", [128, 129])
def test_fiscal_identity_key_length_boundary(setup: PortalSetup, length: int) -> None:
    from apps.cycles.parsing import UnsafeDocument  # noqa: PLC0415

    key = "3" * length
    raw = nfe().replace(KEY.encode(), key.encode())
    if length == 129:
        with pytest.raises(UnsafeDocument, match="missing_fiscal_key"):
            ingest(setup, payload=raw)
        with tenant_context(setup.graph.tenant.pk):
            assert not FiscalDocument.objects.exists()
    else:
        ingest(setup, payload=raw)
        assert document(setup).document_key == key


@pytest.mark.parametrize("terminal", ["cancelada", "substituida"])
def test_capture_lifecycle_metadata_retains_original_and_never_resurrects(
    setup: PortalSetup, terminal: str
) -> None:
    effective = datetime(2026, 9, 16, 14, 30, tzinfo=UTC)
    replacement = "NFS" + "9" * 50 if terminal == "substituida" else ""
    ingest(setup, version=10, payload=nfse(), client_role="tomador")
    initial = document(setup)
    with tenant_context(setup.graph.tenant.pk):
        original = Document.objects.get()
    ingest(
        setup,
        payload=nfse(),
        client_role="tomador",
        fiscal_state=terminal,
        replacement_key=replacement,
        source_effective_at=effective,
    )
    current = document(setup)
    assert current.pk == initial.pk
    assert current.fiscal_state == terminal
    assert revision_of(current).fiscal_state == terminal
    assert revision_of(current).replacement_key == replacement
    assert revision_of(current).source_effective_at == effective
    assert revision_of(current).provenance["status_basis"] == "provider_snapshot"
    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.get().pk == original.pk
        assert Document.objects.get().sha256 == original.sha256
        assert FiscalRevision.objects.count() == 2
        assert RevisionArtifact.objects.filter(document=original).count() == 2
    ingest(
        setup,
        version=13,
        payload=nfse(),
        client_role="tomador",
        fiscal_state="autorizada",
    )
    contradicted = document(setup)
    assert contradicted.current_revision_id == current.current_revision_id
    assert contradicted.fiscal_state == "incerta"


def test_manual_import_cannot_assert_a_provider_lifecycle_state(
    setup: PortalSetup,
) -> None:
    with pytest.raises(ValidationError, match="Contexto fiscal invalido"):
        ingest(
            setup, payload=nfe(), source="importacao_equipe", fiscal_state="cancelada"
        )
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()
        assert not FiscalDocument.objects.exists()


@pytest.mark.parametrize("changed", ["replacement_key", "source_effective_at"])
def test_same_provider_version_cannot_change_lifecycle_semantics(
    setup: PortalSetup, changed: str
) -> None:
    effective = datetime(2026, 9, 16, 14, 30, tzinfo=UTC)
    replacement = "NFS" + "9" * 50
    ingest(
        setup,
        payload=nfse(),
        client_role="tomador",
        fiscal_state="substituida",
        replacement_key=replacement,
        source_effective_at=effective,
    )
    first = document(setup)
    ingest(
        setup,
        payload=nfse(),
        client_role="tomador",
        fiscal_state="substituida",
        replacement_key="NFS" + "8" * 50
        if changed == "replacement_key"
        else replacement,
        source_effective_at=(
            datetime(2026, 9, 16, 15, 30, tzinfo=UTC)
            if changed == "source_effective_at"
            else effective
        ),
    )
    current = document(setup)
    assert current.current_revision_id == first.current_revision_id
    assert current.fiscal_state == "incerta"
    with tenant_context(setup.graph.tenant.pk):
        assert FiscalRevision.objects.count() == 1
        assert CycleException.objects.filter(
            reason_code="incidente_consistencia_fiscal"
        ).exists()
