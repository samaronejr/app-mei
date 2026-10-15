"""Locale state follows the R1 client door, not browser negotiation."""

from http import HTTPStatus

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import issue_client_invite
from apps.accounts.models import User
from apps.cycles.models import RequestState
from apps.tenants.models import Membership, TenantRole
from tests.portal.request_fixtures import portal_setup
from tests.ui.factories import PASSWORD, add_client, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["localhost", ".localhost", "testserver"]


@pytest.mark.parametrize("language", ["en-US,en;q=0.9", "pt-BR,pt;q=0.9"])
def test_invitation_validation_and_mfa_handoff_use_client_locale(language: str) -> None:
    firm = make_firm("locale")
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    company = add_client(firm, legal_name="Padaria MEI", base="112223330001")
    invite, token = issue_client_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        client=company,
        email="cliente@locale.example",
        role=TenantRole.CLIENT_OWNER,
    )
    path = reverse("portal-invite-accept", args=[token], urlconf="apps.portal.urls")
    browser = Client(HTTP_HOST="locale-portal.localhost", HTTP_ACCEPT_LANGUAGE=language)
    browser.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    for response in (browser.get(path), browser.post(path, {})):
        assert response.wsgi_request.LANGUAGE_CODE == "pt-br"
        assert response.headers["Content-Language"] == "pt-br"
    refused = browser.post(path, {})
    assert refused.status_code == HTTPStatus.BAD_REQUEST
    form = refused.context["form"]
    assert form.errors.as_data()["password1"][0].code == "required"
    invite.refresh_from_db()
    assert invite.accepted_at is None
    accepted = browser.post(
        path,
        {"full_name": "Dona Maria", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert accepted.status_code == HTTPStatus.FOUND
    invite.refresh_from_db()
    assert invite.accepted_at is not None
    user = User.objects.get(email=invite.email)
    assert Membership.objects.filter(
        user=user, tenant=firm.tenant, client=company, is_active=True
    ).exists()
    enrolment = browser.get(accepted.url, follow=True)
    assert enrolment.wsgi_request.path == reverse("account_reauthenticate")
    challenge = browser.post(
        enrolment.wsgi_request.get_full_path(), {"password": PASSWORD}
    )
    assert challenge.status_code == HTTPStatus.FOUND
    enrolment = browser.get(challenge.url, follow=True)
    assert enrolment.wsgi_request.path == reverse("mfa_activate_totp")
    assert enrolment.wsgi_request.LANGUAGE_CODE == "pt-br"
    assert browser.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


@pytest.mark.parametrize("language", ["en-US,en;q=0.9", "pt-BR,pt;q=0.9"])
def test_invitation_password_mismatch_reads_in_pt_br(language: str) -> None:
    firm = make_firm("mismatch")
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    company = add_client(firm, legal_name="Padaria MEI", base="112223330002")
    invite, token = issue_client_invite(
        actor=firm.owner,
        tenant=firm.tenant,
        client=company,
        email="cliente@mismatch.example",
        role=TenantRole.CLIENT_OWNER,
    )
    path = reverse("portal-invite-accept", args=[token], urlconf="apps.portal.urls")
    browser = Client(
        HTTP_HOST="mismatch-portal.localhost", HTTP_ACCEPT_LANGUAGE=language
    )
    refused = browser.post(
        path,
        {"full_name": "Dona Maria", "password1": PASSWORD, "password2": PASSWORD + "x"},
    )
    assert refused.status_code == HTTPStatus.BAD_REQUEST
    body = refused.content.decode()
    assert "Os dois campos de senha não correspondem." in body
    assert "did not match" not in body
    invite.refresh_from_db()
    assert invite.accepted_at is None


@pytest.mark.parametrize("language", ["en-US,en;q=0.9", "pt-BR,pt;q=0.9"])
@pytest.mark.parametrize(
    "path",
    ["/accounts/login/", "/accounts/reauthenticate/", "/accounts/password/reset/"],
)
def test_authentication_recovery_uses_r1_locale(path: str, language: str) -> None:
    firm = make_firm("authlocale")
    firm.tenant.features = ["cycles_r1"]
    firm.tenant.save(update_fields=["features"])
    browser = Client(
        HTTP_HOST="authlocale-portal.localhost", HTTP_ACCEPT_LANGUAGE=language
    )
    response = browser.get(path, follow=True)
    assert response.status_code == HTTPStatus.OK
    assert response.wsgi_request.LANGUAGE_CODE == "pt-br"
    assert response.headers["Content-Language"] == "pt-br"


@pytest.mark.parametrize("language", ["en-US,en;q=0.9", "pt-BR,pt;q=0.9"])
def test_request_and_refused_upload_keep_locale_and_retry_state(language: str) -> None:
    setup = portal_setup("localerequest")
    browser = setup.portal()
    browser.defaults["HTTP_ACCEPT_LANGUAGE"] = language
    path = f"/solicitacoes/{setup.request.pk}/"
    response = browser.get(path)
    assert response.status_code == HTTPStatus.OK
    assert response.wsgi_request.LANGUAGE_CODE == "pt-br"
    assert setup.request.due_on is not None
    assert setup.request.due_on.isoformat() in response.content.decode()
    refused = browser.post(f"{path}enviar", {})
    assert refused.status_code == HTTPStatus.BAD_REQUEST
    assert refused.wsgi_request.LANGUAGE_CODE == "pt-br"
    assert setup.reload(setup.request).state == RequestState.REQUESTED
    retry = browser.get(path)
    assert retry.status_code == HTTPStatus.OK
    assert retry.wsgi_request.LANGUAGE_CODE == "pt-br"


@pytest.mark.parametrize(
    ("host", "features", "disabled"),
    [
        ("legacy-portal.localhost", [], []),
        ("legacy-portal.localhost", ["cycles_r1"], ["cycles_r1"]),
        ("legacy.localhost", ["cycles_r1"], []),
        ("localhost", ["cycles_r1"], []),
        ("missing-portal.localhost", ["cycles_r1"], []),
    ],
)
def test_other_doors_keep_english_negotiation(
    settings: SettingsWrapper,
    host: str,
    features: list[str],
    disabled: list[str],
) -> None:
    firm = make_firm("legacy")
    firm.tenant.features = features
    firm.tenant.save(update_fields=["features"])
    settings.AXORIM_DISABLED_FEATURES = disabled
    browser = Client(HTTP_HOST=host, HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")
    response = browser.get("/accounts/login/")
    assert response.status_code == HTTPStatus.OK
    assert response.wsgi_request.LANGUAGE_CODE == "en"
    assert response.headers["Content-Language"] == "en"
