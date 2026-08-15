from http import HTTPStatus
from smtplib import SMTPException
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import Mock

import pytest
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.invites import revoke_invite
from apps.accounts.models import User
from apps.audit.models import AuditAction, PlatformEvent
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from tests.support import enrol_totp

if TYPE_CHECKING:
    from apps.accounts.forms import InviteIssueForm, PortalInviteIssueForm

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105
INVITED = "novo@alpha.example"
TENANT_HOST = "alpha.localhost"
SEND_FAILURE = (
    "Não foi possível enviar o convite por e-mail. Nada foi criado; tente novamente."
)


class _TestResponse(Protocol):
    status_code: int
    content: bytes
    context: dict[str, object]

    def __getitem__(self, header: str) -> str: ...


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha Contabilidade", slug="alpha")


@pytest.fixture
def owner(tenant: Tenant) -> User:
    user = User.objects.create_user(
        email="owner@alpha.example",
        password=PASSWORD,
    )
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(user)
    return user


@pytest.fixture
def client_company(tenant: Tenant) -> ClientCompany:
    with tenant_context(tenant.pk):
        # ALL_OBJECTS_OK: fixture seeding inside the explicit tenant context above.
        return ClientCompany.all_objects.create(
            tenant=tenant,
            legal_name="PADARIA ALPHA",
            cnpj="11222333000181",
            is_mei=True,
        )


def _post_firm_invite(owner: User) -> _TestResponse:
    session = Client(raise_request_exception=False)
    session.force_login(owner)
    return cast(
        "_TestResponse",
        session.post(
            "/convites/",
            {"email": INVITED, "role": TenantRole.STAFF_ACCOUNTANT},
            headers={"host": TENANT_HOST},
        ),
    )


def _post_portal_invite(owner: User, client_company: ClientCompany) -> _TestResponse:
    session = Client(raise_request_exception=False)
    session.force_login(owner)
    return cast(
        "_TestResponse",
        session.post(
            f"/clientes/{client_company.pk}/convites/",
            {"email": INVITED, "role": TenantRole.CLIENT_OWNER},
            headers={"host": TENANT_HOST},
        ),
    )


def _raise_smtp_error(*_args: object, **_kwargs: object) -> None:
    message = "SMTP indisponível"
    raise SMTPException(message)


def test_smtp_failure_leaves_zero_firm_invite_rows_and_renders_the_form(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_exception = Mock()
    monkeypatch.setattr("apps.accounts.views.send_mail", _raise_smtp_error)
    monkeypatch.setattr("sentry_sdk.capture_exception", capture_exception)

    before = Invite.objects.count()
    response = _post_firm_invite(owner)

    assert Invite.objects.count() == before == 0, "failed delivery left an Invite row"
    failed_issue_events = PlatformEvent.objects.filter(
        action=AuditAction.INVITE_ISSUED,
        subject=INVITED,
    )
    assert failed_issue_events.count() == 1
    assert response.status_code == HTTPStatus.OK
    assert SEND_FAILURE in response.content.decode()
    form = cast("InviteIssueForm", response.context["form"])
    assert list(form.non_field_errors()) == [SEND_FAILURE]
    capture_exception.assert_called_once_with()


def test_invite_issued_renders_only_after_a_successful_send(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_mail = Mock(return_value=1)
    monkeypatch.setattr("apps.accounts.views.send_mail", send_mail)

    response = _post_firm_invite(owner)

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"] == "/convites/enviado/"
    confirmation = Client().get(response["Location"], headers={"host": TENANT_HOST})
    assert confirmation.status_code == HTTPStatus.OK
    assert "Convite enviado" in confirmation.content.decode()
    send_mail.assert_called_once()


def test_a_successful_reissue_after_delivery_recovers_creates_one_live_invite(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_mail = Mock(side_effect=[SMTPException("SMTP indisponível"), 1])
    capture_exception = Mock()
    monkeypatch.setattr("apps.accounts.views.send_mail", send_mail)
    monkeypatch.setattr("sentry_sdk.capture_exception", capture_exception)

    failed = _post_firm_invite(owner)
    assert failed.status_code == HTTPStatus.OK
    assert Invite.objects.count() == 0, "failed delivery left an Invite row"

    recovered = _post_firm_invite(owner)

    assert recovered.status_code == HTTPStatus.FOUND
    live = Invite.objects.filter(
        email=INVITED,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
    )
    assert live.count() == 1
    assert send_mail.call_count == 2
    capture_exception.assert_called_once_with()


def test_two_successful_issues_for_one_email_are_independently_revocable(
    owner: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send_mail = Mock(return_value=1)
    monkeypatch.setattr("apps.accounts.views.send_mail", send_mail)

    responses = [_post_firm_invite(owner), _post_firm_invite(owner)]

    assert [response.status_code for response in responses] == [
        HTTPStatus.FOUND,
        HTTPStatus.FOUND,
    ]
    live = list(
        Invite.objects.filter(
            email=INVITED,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        )
    )
    assert len(live) == 2
    assert live[0].pk != live[1].pk

    for invite in live:
        revoked = revoke_invite(actor=owner, invite=invite)
        assert revoked.revoked_at is not None

    assert Invite.objects.filter(email=INVITED, revoked_at__isnull=False).count() == 2
    assert send_mail.call_count == 2


@pytest.mark.parametrize(
    "delivery_error",
    [
        pytest.param(SMTPException("SMTP indisponível"), id="smtp"),
        pytest.param(OSError("socket indisponível"), id="oserror"),
    ],
)
def test_portal_delivery_failure_leaves_zero_rows_and_allows_a_clean_reissue(
    owner: User,
    client_company: ClientCompany,
    monkeypatch: pytest.MonkeyPatch,
    delivery_error: Exception,
) -> None:
    send_mail = Mock(side_effect=[delivery_error, 1])
    capture_exception = Mock()
    monkeypatch.setattr("apps.accounts.views.send_mail", send_mail)
    monkeypatch.setattr("sentry_sdk.capture_exception", capture_exception)

    failed = _post_portal_invite(owner, client_company)

    assert Invite.objects.count() == 0, "failed portal delivery left an Invite row"
    failed_issue_events = PlatformEvent.objects.filter(
        action=AuditAction.INVITE_ISSUED,
        subject=INVITED,
    )
    assert failed_issue_events.count() == 1
    assert failed.status_code == HTTPStatus.OK
    assert SEND_FAILURE in failed.content.decode()
    form = cast("PortalInviteIssueForm", failed.context["portal_invite_form"])
    assert list(form.non_field_errors()) == [SEND_FAILURE]
    capture_exception.assert_called_once_with()

    recovered = _post_portal_invite(owner, client_company)

    assert recovered.status_code == HTTPStatus.FOUND
    live = Invite.objects.filter(
        email=INVITED,
        client=client_company,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
    )
    assert live.count() == 1
    assert send_mail.call_count == 2
