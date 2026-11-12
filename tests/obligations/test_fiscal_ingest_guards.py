"""Assertion witnesses for fiscal routing, authorization and retained provenance."""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import Event
from apps.authz.models import GrantLevel, Role, RoleGrant
from apps.authz.services import can
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles.evidence import DocumentIntake, ExtractedField, IntakeState
from apps.cycles.extraction.service import extract_intake
from apps.cycles.extraction.v2 import extract
from apps.cycles.intake import vault_upload
from apps.cycles.parsing import UnsafeDocument
from apps.cycles.scanning import ByteStream, FakeScanner, Verdict, scan_intake
from apps.integrations.models import ClientProviderLink
from apps.obligations import fiscal_ingest
from apps.obligations.models import Document, FiscalDocument, RevisionArtifact
from apps.tenants.models import Membership
from tests.obligations.fiscal_fixtures import (
    ALPHANUMERIC,
    CLIENT,
    COUNTERPART,
    KEY,
    nfe,
    nfse,
    summary,
)
from tests.obligations.test_fiscal_documents import (
    binding,
    document,
    ingest,
    revision_of,
    setup,
)
from tests.portal.request_fixtures import PortalSetup

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["binding", "setup"]


def test_counterpart_roles_follow_the_declared_fiscal_directions() -> None:
    assert fiscal_ingest.ROLE_COUNTERPART == {
        "destinatario": "emitente",
        "emitente": "destinatario",
        "tomador": "prestador",
        "prestador": "tomador",
    }


@pytest.mark.parametrize("role", ["emitente", "prestador", "tomador"])
def test_each_client_role_preserves_its_counterpart(
    setup: PortalSetup, role: str
) -> None:
    if role == "emitente":
        payload = nfe(issuer=CLIENT, recipient=COUNTERPART)
        expected = (COUNTERPART, "Synthetic recipient")
    elif role == "prestador":
        payload = (
            nfse()
            .replace(ALPHANUMERIC.encode(), CLIENT.encode())
            .replace(
                f"<toma><CNPJ>{CLIENT}</CNPJ>".encode(),
                f"<toma><CNPJ>{COUNTERPART}</CNPJ>".encode(),
            )
        )
        expected = (COUNTERPART, "Synthetic tomador")
    else:
        payload = nfse()
        expected = (ALPHANUMERIC, "Synthetic prestador")
    ingest(setup, payload=payload, client_role=role)
    current = document(setup)
    assert current.client_role == role
    assert current.role_verification == "confirmed"
    assert (current.counterpart_id, current.counterpart_name) == expected


def test_register_locks_client_binding_head_and_intake(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    with CaptureQueriesContext(connection) as captured:
        ingest(setup, head_summary=summary(), binding=binding)
    queries = [row["sql"] for row in captured.captured_queries]
    clients = [sql for sql in queries if 'FROM "clients_clientcompany"' in sql]
    links = [sql for sql in queries if 'FROM "integrations_clientproviderlink"' in sql]
    assert clients
    assert "FOR UPDATE" in clients[0]
    assert links
    assert all("FOR UPDATE" in sql for sql in links)
    heads = [
        index
        for index, sql in enumerate(queries)
        if 'FROM "obligations_fiscaldocument"' in sql and "FOR UPDATE OF" in sql
    ]
    assert heads
    assert any(
        'FROM "cycles_documentintake"' in sql and "FOR UPDATE" in sql
        for sql in queries[heads[0] + 1 :]
    )


def test_authorization_queries_only_the_named_tenant(setup: PortalSetup) -> None:
    manager = fiscal_ingest.Tenant.objects
    with patch.object(manager, "get", wraps=manager.get) as selected:
        ingest(setup, payload=nfe())
    assert selected.call_args_list
    assert all(
        call.args == () and call.kwargs == {"pk": setup.graph.tenant.pk}
        for call in selected.call_args_list
    )


def test_register_audit_refs_match_the_retained_revision(setup: PortalSetup) -> None:
    ingest(setup, payload=nfe())
    current = document(setup)
    with tenant_context(setup.graph.tenant.pk):
        for action in ("fiscal.head_changed", "fiscal.revision_registered"):
            event = Event.objects.get(action=action)
            assert (event.object_type, event.object_id) == (
                "FiscalRevision",
                str(revision_of(current).pk),
            )
            assert event.metadata == {
                "document_id": str(current.pk),
                "role_verification": "confirmed",
            }


def test_visible_staff_still_requires_the_cycle_operation_capability(
    setup: PortalSetup,
) -> None:
    grant = RoleGrant.objects.get(role=Role.OWNER, capability__slug="cycles.operate")
    previous = grant.level
    grant.level = GrantLevel.NONE
    grant.save(update_fields=["level"])
    try:
        assert not can(
            setup.graph.actor, "cycles.operate", tenant_id=setup.graph.tenant.pk
        )
        with pytest.raises(PermissionDenied):
            ingest(setup, payload=nfe(), source="importacao_equipe")
        with tenant_context(setup.graph.tenant.pk):
            assert not Document.objects.exists()
            assert not FiscalDocument.objects.exists()
    finally:
        grant.level = previous
        grant.save(update_fields=["level"])


@pytest.mark.parametrize("change", ["staff", "active", "client", "user"])
def test_authorization_is_rechecked_after_scanner_io(
    setup: PortalSetup, change: str
) -> None:
    if change == "staff":
        with tenant_context(setup.graph.tenant.pk):
            ClientAssignment.objects.create(
                tenant=setup.graph.tenant,
                client=setup.graph.client,
                user=setup.graph.staff,
            )
        assert can(setup.graph.staff, "cycles.operate", tenant_id=setup.graph.tenant.pk)
    scanner = FakeScanner()
    scan = scanner.scan

    def revoke(_scanner: FakeScanner, stream: ByteStream) -> Verdict:
        verdict = scan(stream)
        with tenant_context(setup.graph.tenant.pk):
            if change == "staff":
                ClientAssignment.objects.filter(
                    client=setup.graph.client, user=setup.graph.staff
                ).delete()
            else:
                membership = Membership.objects.get(
                    tenant=setup.graph.tenant,
                    client=setup.graph.client,
                    user=setup.portal_user,
                )
                if change == "client":
                    membership.client = setup.other_client
                    membership.save(update_fields=["client"])
                else:
                    membership.is_active = False
                    membership.save(update_fields=["is_active"])
                    if change == "user":
                        alternate = User.objects.create_user(
                            email=f"alternate-{setup.graph.tenant.pk}@fiscal.test"
                        )
                        Membership.objects.create(
                            tenant=setup.graph.tenant,
                            client=setup.graph.client,
                            user=alternate,
                            role=membership.role,
                            is_active=True,
                        )
        return verdict

    expected = Http404 if change == "staff" else PermissionDenied
    with (
        patch.object(FakeScanner, "scan", autospec=True, side_effect=revoke),
        pytest.raises(expected),
    ):
        ingest(
            setup,
            payload=nfe(),
            source="importacao_equipe" if change == "staff" else "upload_cliente",
            actor=setup.graph.staff if change == "staff" else setup.portal_user,
            scanner=scanner,
        )
    with tenant_context(setup.graph.tenant.pk):
        assert Document.objects.count() == 1
        assert not FiscalDocument.objects.exists()


def test_recipientless_xml_cannot_borrow_a_capture_link_for_staff(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    payload = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<resNFe xmlns="http://www.portalfiscal.inf.br/nfe">'
        f"<chNFe>{KEY}</chNFe><CNPJ>{COUNTERPART}</CNPJ>"
        "<xNome>Synthetic issuer</xNome><dhEmi>2026-09-15T12:30:00-03:00</dhEmi>"
        "<vNF>1500.25</vNF><cSitNFe>1</cSitNFe></resNFe>"
    ).encode()
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, payload=payload, source="importacao_equipe", binding=binding)
    with tenant_context(setup.graph.tenant.pk):
        assert not FiscalDocument.objects.exists()


def test_matching_non_focus_binding_cannot_supply_a_summary(
    setup: PortalSetup, binding: ClientProviderLink
) -> None:
    with tenant_context(setup.graph.tenant.pk):
        binding.connection.provider = "serpro"
        binding.connection.save(update_fields=["provider"])
    with pytest.raises(ValidationError, match="vinculo de captura"):
        ingest(setup, head_summary=summary(), binding=binding, provider="serpro")
    with tenant_context(setup.graph.tenant.pk):
        assert not Document.objects.exists()


@pytest.mark.parametrize("source", ["importacao_equipe", "upload_cliente"])
def test_human_original_keeps_its_fiscal_filename(
    setup: PortalSetup, source: str
) -> None:
    ingest(setup, payload=nfe(), source=source)
    with tenant_context(setup.graph.tenant.pk):
        original = Document.objects.get()
        assert original.original_filename == "documento-fiscal.xml"
        assert original.content_type == "application/xml"


def test_v1_fields_and_stale_reason_do_not_prevent_v2_classification(
    setup: PortalSetup,
) -> None:
    payload = nfe(issuer=CLIENT)
    with tenant_context(setup.graph.tenant.pk):
        original, _ = vault_upload(
            setup.portal_user,
            SimpleUploadedFile("documento-fiscal.xml", payload),
            tenant=setup.graph.tenant,
            client=setup.graph.client,
        )
        intake = DocumentIntake.objects.get(document=original)
    scan_intake(
        setup.graph.tenant.pk, intake.pk, scanner=FakeScanner(), now=timezone.now()
    )
    extract_intake(setup.graph.tenant.pk, intake.pk, now=timezone.now())
    with tenant_context(setup.graph.tenant.pk):
        intake = DocumentIntake.objects.get(pk=intake.pk)
        assert set(
            ExtractedField.objects.filter(document=original).values_list(
                "extractor_version", flat=True
            )
        ) == {"r1.1"}
        intake.reason = "stale_v1_classification"
        intake.save(update_fields=["reason"])
        previous_version = intake.version
    ingest(setup, payload=payload, source="upload_cliente")
    with tenant_context(setup.graph.tenant.pk):
        assert set(
            ExtractedField.objects.filter(document=original).values_list(
                "extractor_version", flat=True
            )
        ) == {"r1.1", "2"}
        refreshed = DocumentIntake.objects.get(pk=intake.pk)
        assert refreshed.state == IntakeState.CLASSIFIED
        assert refreshed.reason == ""
        assert refreshed.version == previous_version + 1


@pytest.mark.parametrize("metadata", ["replacement", "effective"])
def test_lifecycle_digest_names_and_preserves_each_optional_semantic(
    setup: PortalSetup, metadata: str
) -> None:
    effective = (
        datetime(2026, 9, 16, 14, 30, tzinfo=UTC) if metadata == "effective" else None
    )
    replacement = "NFS" + "9" * 50 if metadata == "replacement" else ""
    state = "substituida" if metadata == "replacement" else "cancelada"
    payload = nfse()
    ingest(
        setup,
        payload=payload,
        client_role="tomador",
        fiscal_state=state,
        replacement_key=replacement,
        source_effective_at=effective,
    )
    semantics = {
        "family": "nfse_nacional",
        "key": f"NFS{KEY}",
        "state": state,
        "fields": {
            field.name: field.value
            for field in extract(
                DocumentIntake(state=IntakeState.VERIFIED), payload
            ).fields
        },
        "summary_only": False,
        "note": "",
        "replacement_key": replacement,
        "source_effective_at": effective.isoformat() if effective is not None else None,
    }
    expected = hashlib.sha256(
        json.dumps(semantics, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    revision = revision_of(document(setup))
    assert revision.semantic_digest == expected
    assert revision.replacement_key == replacement
    assert revision.source_effective_at == effective


def test_provider_versions_do_not_order_different_documents(setup: PortalSetup) -> None:
    ingest(setup, version=12, payload=nfe())
    other_key = "35" + "0" * 42
    ingest(setup, version=1, payload=nfe().replace(KEY.encode(), other_key.encode()))
    with tenant_context(setup.graph.tenant.pk):
        other = FiscalDocument.objects.select_related("current_revision").get(
            document_key=other_key
        )
        assert revision_of(other).provider_version == "1"
        assert other.fiscal_state == "autorizada"


@pytest.mark.parametrize("terminal", ["cancelada", "substituida"])
def test_a_newer_confirmation_of_the_same_terminal_state_is_not_an_incident(
    setup: PortalSetup, terminal: str
) -> None:
    for version in (12, 13):
        ingest(
            setup,
            version=version,
            payload=nfse(),
            client_role="tomador",
            fiscal_state=terminal,
        )
    current = document(setup)
    assert current.fiscal_state == terminal
    assert revision_of(current).provider_version == "13"


def test_missing_counterpart_fields_stay_empty(setup: PortalSetup) -> None:
    payload = nfe().replace(
        (
            f"<emit><CNPJ>{COUNTERPART}</CNPJ><xNome>Synthetic issuer</xNome></emit>"
        ).encode(),
        b"",
    )
    ingest(setup, payload=payload)
    current = document(setup)
    assert current.role_verification == "confirmed"
    assert (current.counterpart_id, current.counterpart_name) == ("", "")


def test_missing_national_key_is_refused_before_identity_creation(
    setup: PortalSetup,
) -> None:
    with pytest.raises(UnsafeDocument, match="missing_fiscal_key"):
        ingest(
            setup,
            payload=nfse().replace(f'Id="NFS{KEY}"'.encode(), b""),
            client_role="tomador",
        )
    with tenant_context(setup.graph.tenant.pk):
        assert not FiscalDocument.objects.exists()
        assert not RevisionArtifact.objects.exists()
