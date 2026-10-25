"""Authority admission must honor the current tenant, client and evidence scope."""

from datetime import timedelta

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError

from apps.core.tenancy import tenant_context
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.obligations.models import AcquisitionOrigin, Document
from tests.integrations.test_authority import TODAY, authority_api
from tests.ui.factories import make_firm

pytestmark = pytest.mark.django_db


def test_authority_lookup_never_borrows_another_client_or_tenants_record() -> None:
    api = authority_api()
    alpha = make_firm("authority-alpha", client_count=2)
    beta = make_firm("authority-beta", client_count=1)
    for firm in (alpha, beta):
        firm.tenant.features = ["cycles_r1", "scan_required", "federal_checks"]
        firm.tenant.save(update_fields=["features"])
    with tenant_context(alpha.tenant.pk):
        api.declare_authority(
            alpha.owner,
            alpha.clients[0].pk,
            service="pagtoweb",
            code="00004",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        assert (
            api.authority_for(alpha.clients[1], "SP-PAG", today=TODAY).state
            == "missing"
        )
    with tenant_context(beta.tenant.pk):
        assert (
            api.authority_for(beta.clients[0], "SP-PAG", today=TODAY).state == "missing"
        )
        # A caller holding a foreign client object cannot bypass the active tenant.
        assert (
            api.authority_for(alpha.clients[0], "SP-PAG", today=TODAY).state != "usable"
        )


@pytest.mark.parametrize("flaw", ["other_client", "portal_visible", "portal_origin"])
def test_evidence_must_be_private_staff_upload_of_the_same_client(flaw: str) -> None:
    api = authority_api()
    firm = make_firm("authority-evidence", client_count=2)
    firm.tenant.features = ["cycles_r1", "scan_required", "federal_checks"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.pk):
        document = Document.objects.create(
            tenant=firm.tenant,
            client=firm.clients[1] if flaw == "other_client" else firm.clients[0],
            original_filename="authority.pdf",
            content_type="application/pdf",
            byte_size=10,
            sha256="a" * 64,
            uploaded_by=firm.owner,
            acquisition_origin=(
                AcquisitionOrigin.UPLOAD_PORTAL
                if flaw == "portal_origin"
                else AcquisitionOrigin.UPLOAD_STAFF
            ),
            portal_visible=flaw == "portal_visible",
        )
        with pytest.raises(ValidationError):
            api.declare_authority(
                firm.owner,
                firm.clients[0].pk,
                service="pagtoweb",
                code="00004",
                valid_from=TODAY,
                expected_version=0,
                evidence_document=document,
                today=TODAY,
            )
        assert not apps.get_model("integrations", "AuthorityRecord").objects.exists()


def test_invalid_date_range_has_no_authority_or_audit_side_effects() -> None:
    api = authority_api()
    firm = make_firm("authority-dates", client_count=1)
    firm.tenant.features = ["cycles_r1", "scan_required", "federal_checks"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.pk), pytest.raises(ValidationError):
        api.declare_authority(
            firm.owner,
            firm.clients[0].pk,
            service="pagtoweb",
            code="00004",
            valid_from=TODAY + timedelta(days=10),
            valid_to=TODAY + timedelta(days=5),
            expected_version=0,
            today=TODAY,
        )
    with tenant_context(firm.tenant.pk):
        assert not apps.get_model("integrations", "AuthorityRecord").objects.exists()


@pytest.mark.parametrize(
    ("state", "days", "generation", "foreign", "expected"),
    [
        ("ativa", -1, 1, False, ("expired", "certificado_vencido")),
        ("divergente", 90, 1, False, ("missing", "credencial_divergente")),
        ("rotacao_pendente", 90, 1, False, ("missing", "certificado_rotacao_pendente")),
        ("ausente", 90, 0, False, ("missing", "certificado_ausente")),
        ("ativa", 90, 1, True, ("missing", "credencial_divergente")),
        ("ativa", 90, 1, False, ("usable", "ativa")),
    ],
)
def test_capture_needs_a_current_usable_certificate(
    state: str,
    days: int,
    generation: int,
    foreign: bool,
    expected: tuple[str, str],
) -> None:
    api = authority_api()
    firm = make_firm("authority-certificate", client_count=1)
    firm.tenant.features = ["cycles_r1", "scan_required", "document_capture"]
    firm.tenant.save(update_fields=["features"])
    company = firm.clients[0]
    with tenant_context(firm.tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=firm.tenant,
            provider="focus",
            environment="fake",
            status="ativa",
        )
        ClientProviderLink.objects.create(
            tenant=firm.tenant,
            client=company,
            connection=connection,
            provider_company_ref="capture",
            streams=["nfe_recebidas"],
            status="ativa",
            credential_state=state,
            certificate_cnpj="11444777000161" if foreign else company.cnpj,
            certificate_valid_from=TODAY - timedelta(days=1),
            certificate_valid_to=TODAY + timedelta(days=days),
            credential_generation=generation,
        )
        api.declare_authority(
            firm.owner,
            company.pk,
            service="captura_nfe",
            code="autorizacao_cliente",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        decision = api.authority_for(company, "FC-NFE", today=TODAY)
        assert (decision.state, decision.reason) == expected


def test_unexpected_generation_is_divergent_even_when_cnpj_matches() -> None:
    api = authority_api()
    firm = make_firm("authority-generation", client_count=1)
    firm.tenant.features = ["cycles_r1", "scan_required", "document_capture"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=firm.tenant,
            provider="focus",
            environment="fake",
            status="ativa",
        )
        link = ClientProviderLink.objects.create(
            tenant=firm.tenant,
            client=firm.clients[0],
            connection=connection,
            provider_company_ref="generation",
            streams=["nfe_recebidas"],
            status="ativa",
            credential_state="ativa",
            credential_generation=1,
        )
        api.record_certificate_status(
            link.pk,
            expected_version=1,
            certificate_cnpj=firm.clients[0].cnpj,
            valid_from=TODAY,
            valid_to=TODAY + timedelta(days=90),
            credential_generation=2,
            today=TODAY,
        )
        link.refresh_from_db()
        assert link.credential_state == "divergente"
        assert link.credential_generation == 1


@pytest.mark.parametrize(
    (
        "current",
        "valid_to_days",
        "reported_days",
        "generation",
        "expected",
        "expected_generation",
    ),
    [
        # Forbidden by A7.3 on the same certificate: validity only moves forward through
        # rotation, so each of these is drift and must block capture.
        pytest.param("vencendo", 10, 200, 1, "divergente", 1, id="vencendo-to-ativa"),
        pytest.param("vencida", -1, 200, 1, "divergente", 1, id="vencida-to-ativa"),
        pytest.param("vencida", -1, 10, 1, "divergente", 1, id="vencida-to-vencendo"),
        # Allowed moves, as controls.
        pytest.param("ativa", 200, 10, 1, "vencendo", 1, id="ativa-to-vencendo"),
        pytest.param("vencendo", 10, -1, 1, "vencida", 1, id="vencendo-to-vencida"),
        pytest.param(
            "rotacao_pendente", -1, 200, 2, "ativa", 2, id="rotation-to-ativa"
        ),
    ],
)
def test_a_reported_certificate_only_moves_along_the_credential_machine(  # noqa: PLR0913, PLR0917
    current: str,
    valid_to_days: int,
    reported_days: int,
    generation: int,
    expected: str,
    expected_generation: int,
) -> None:
    api = authority_api()
    firm = make_firm("authority-credential", client_count=1)
    firm.tenant.features = ["cycles_r1", "scan_required", "document_capture"]
    firm.tenant.save(update_fields=["features"])
    company = firm.clients[0]
    with tenant_context(firm.tenant.pk):
        connection = ProviderConnection.objects.create(
            tenant=firm.tenant,
            provider="focus",
            environment="fake",
            status="ativa",
        )
        link = ClientProviderLink.objects.create(
            tenant=firm.tenant,
            client=company,
            connection=connection,
            provider_company_ref="credential",
            streams=["nfe_recebidas"],
            status="ativa",
            credential_state=current,
            credential_generation=1,
            certificate_cnpj=company.cnpj,
            certificate_valid_to=TODAY + timedelta(days=valid_to_days),
        )
        api.declare_authority(
            firm.owner,
            company.pk,
            service="captura_nfe",
            code="autorizacao_cliente",
            valid_from=TODAY,
            expected_version=0,
            today=TODAY,
        )
        api.record_certificate_status(
            link.pk,
            expected_version=1,
            certificate_cnpj=company.cnpj,
            valid_from=TODAY,
            valid_to=TODAY + timedelta(days=reported_days),
            credential_generation=generation,
            today=TODAY,
        )
        link.refresh_from_db()
        assert (link.credential_state, link.credential_generation) == (
            expected,
            expected_generation,
        )
        drift = apps.get_model("integrations", "IntegrationIssue").objects.filter(
            reason="credencial_divergente",
            link=link,
            state="aberta",
        )
        decision = api.authority_for(company, "FC-NFE", today=TODAY, link=link)
        if expected == "divergente":
            assert drift.exists()
            assert (decision.state, decision.reason) == (
                "missing",
                "credencial_divergente",
            )
        else:
            assert not drift.exists()
            assert decision.reason != "credencial_divergente"
