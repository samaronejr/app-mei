"""The existing client workspace owns authority actions; the portal never does."""

import re
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from tests.ui.factories import Firm, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def firm() -> Firm:
    firm = make_firm("authority-workspace", client_count=1)
    firm.owner.full_name = "Ana"
    firm.owner.save(update_fields=["full_name"])
    firm.tenant.features = ["cycles_r1", "scan_required", "federal_checks"]
    firm.tenant.save(update_fields=["features"])
    return firm


def _url(firm: Firm) -> str:
    return f"/clientes/{firm.clients[0].pk}/"


def _declare(browser: Client, firm: Firm) -> None:
    response = browser.post(
        f"{_url(firm)}autoridades/pagtoweb/declarar/",
        {
            "expected_version": 0,
            "code": "00004",
            "valid_from": "2026-01-01",
            "valid_to": "2027-12-31",
            "evidence": SimpleUploadedFile(
                "procuracao.pdf",
                b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF",
                content_type="application/pdf",
            ),
        },
    )
    assert response.status_code == 302


def test_workspace_declares_verifies_and_revokes_private_evidence(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    settings.CLAMAV_ENABLED = True
    browser = firm.as_owner()
    response = browser.get(_url(firm))
    assert response.status_code == 200
    assert "data-authority-panel" in response.content.decode()
    _declare(browser, firm)
    html = browser.get(_url(firm)).content.decode()
    assert "Declarada (evidência anexada)" in html
    assert (
        'data-state="confirmed"'
        not in html.split("data-authority-panel", 1)[1].split("</section>", 1)[0]
    )
    response = browser.post(
        f"{_url(firm)}autoridades/pagtoweb/verificar/",
        {"expected_version": 1},
    )
    assert response.status_code == 302
    html = browser.get(_url(firm)).content.decode()
    assert "Verificada por Ana em" in html
    response = browser.post(
        f"{_url(firm)}autoridades/pagtoweb/revogar/",
        {"expected_version": 2},
    )
    assert response.status_code == 302
    assert "Revogada" in browser.get(_url(firm)).content.decode()


def test_stale_rendered_form_returns_conflict_without_another_revision(
    firm: Firm,
    settings: SettingsWrapper,
) -> None:
    settings.CLAMAV_ENABLED = True
    browser = firm.as_owner()
    _declare(browser, firm)
    response = browser.post(
        f"{_url(firm)}autoridades/pagtoweb/verificar/",
        {"expected_version": 0},
    )
    assert response.status_code == 409
    from apps.integrations.models import AuthorityRecord  # noqa: PLC0415

    with tenant_context(firm.tenant.pk):
        assert AuthorityRecord.objects.count() == 1


def test_authority_panel_is_flag_gated_and_staff_has_no_write_actions(
    firm: Firm,
) -> None:
    assign(firm, firm.clients[0], firm.accountant)
    html = firm.as_accountant().get(_url(firm)).content.decode()
    assert "data-authority-panel" in html
    panel = html.split("data-authority-panel", 1)[1].split("</section>", 1)[0]
    assert "<form" not in panel
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    owner = firm.as_owner()
    assert "data-authority-panel" not in owner.get(_url(firm)).content.decode()
    response = owner.post(
        f"{_url(firm)}autoridades/pagtoweb/declarar/",
        {"expected_version": 0, "code": "00004", "valid_from": "2026-01-01"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("client.pfx", b"synthetic certificate bytes"),
        # PDF magic under a certificate name: the name alone is refused.
        ("client.p12", b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"),
        # A certificate-looking payload under an innocent name: the bytes refuse it.
        ("procuracao.pdf", b"0\x82\x05\x00synthetic"),
        # Intake would accept XML; authority evidence is a PDF or an image only.
        ("procuracao.xml", b"<?xml version='1.0'?><procuracao/>"),
    ],
)
def test_certificate_upload_is_refused_before_a_document_is_stored(
    firm: Firm,
    settings: SettingsWrapper,
    name: str,
    payload: bytes,
) -> None:
    from apps.obligations.models import Document  # noqa: PLC0415

    settings.CLAMAV_ENABLED = True
    browser = firm.as_owner()
    with tenant_context(firm.tenant.pk):
        before = Document.objects.count()
    response = browser.post(
        f"{_url(firm)}autoridades/pagtoweb/declarar/",
        {
            "expected_version": 0,
            "code": "00004",
            "valid_from": date(2026, 1, 1).isoformat(),
            "evidence": SimpleUploadedFile(
                name, payload, content_type="application/x-pkcs12"
            ),
        },
    )
    assert response.status_code == 422
    with tenant_context(firm.tenant.pk):
        assert Document.objects.count() == before


def _authority_row(html: str, service: str) -> str:
    match = re.search(
        rf'data-authority-service="{service}".*?</li>', html, flags=re.DOTALL
    )
    assert match, f"no {service} row"
    return match.group(0)


def test_a_lapsed_pending_authority_offers_renewal_not_an_expired_verification(
    firm: Firm,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.integrations.authority import authority_for  # noqa: PLC0415
    from apps.integrations.models import AuthorityRecord  # noqa: PLC0415
    from tests.integrations.test_authority import authority_api  # noqa: PLC0415

    settings.CLAMAV_ENABLED = True
    api = authority_api()
    company = firm.clients[0]
    declared_on, lapses_on, viewed_on = (
        date(2026, 11, 10),
        date(2026, 11, 15),
        date(2026, 11, 30),
    )
    with tenant_context(firm.tenant.pk):
        api.declare_authority(
            firm.owner,
            company.pk,
            service="sitfis",
            code="00002",
            valid_from=declared_on,
            expected_version=0,
            today=declared_on,
        )
        api.revoke_authority(
            firm.owner,
            company.pk,
            service="sitfis",
            expected_version=1,
            today=declared_on,
        )
        api.declare_authority(
            firm.owner,
            company.pk,
            service="sitfis",
            code="00002",
            valid_from=declared_on,
            valid_to=lapses_on,
            expected_version=2,
            today=declared_on,
        )
    monkeypatch.setattr(timezone, "localdate", lambda *_args, **_kwargs: viewed_on)
    browser = firm.as_owner()

    row = _authority_row(browser.get(_url(firm)).content.decode(), "sitfis")
    assert "Vencida sem verificação" in row
    assert "autoridades/sitfis/declarar/" in row
    assert 'name="expected_version" value="3"' in row
    assert "autoridades/sitfis/verificar/" not in row

    response = browser.post(
        f"{_url(firm)}autoridades/sitfis/declarar/",
        {"expected_version": 3, "code": "00002", "valid_from": viewed_on.isoformat()},
    )
    assert response.status_code == 302
    with tenant_context(firm.tenant.pk):
        renewed = AuthorityRecord.objects.filter(
            client=company, service="sitfis"
        ).latest("version")
        assert (renewed.version, renewed.status, renewed.verified_by_id) == (
            4,
            "verificacao_pendente",
            None,
        )
        assert authority_for(company, "SP-SIT-P", today=viewed_on).state == "unknown"
    row = _authority_row(browser.get(_url(firm)).content.decode(), "sitfis")
    assert "autoridades/sitfis/verificar/" in row
    assert "autoridades/sitfis/declarar/" not in row
