"""The admin tenant selector, which is a cross-tenant read primitive unless authorized.

The console is served on the platform hostname, so no tenant is resolved and one must
be chosen. The danger is that choosing is invisible to both isolation layers *by
design*: once the GUC and the ContextVar say tenant B, row-level security correctly
returns tenant B's rows and the scoped manager correctly agrees. Nothing is violated.
The only thing standing between one `is_staff` account and every firm's CNPJs, CPFs
and revenue is the server-side check in `apps.tenants.admin_tenancy.authorize`.

`test_a_staff_user_cannot_select_a_firm_they_do_not_belong_to` is that check, driven
through HTTP with the tenant id submitted as a form field — which is how it arrives in
reality, and why filtering the dropdown would change nothing.
"""

from http import HTTPStatus

import pytest
from django.contrib.auth.models import Permission
from django.http.response import HttpResponseBase
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.core.tenancy import tenant_context
from apps.tenants.admin_tenancy import ADMIN_TENANT_SESSION_KEY
from apps.tenants.models import Membership, Tenant, TenantRole
from apps.tenants.support import (
    SupportApproval,
    approve_support_grant,
    request_support_grant,
)
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

SELECT_URL = "/admin/selecionar-empresa/"
EVENT_CHANGELIST = "/admin/audit/event/"
ALPHA_MARKER = "alpha-only-marker"
BETA_MARKER = "beta-only-marker"


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.admin.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


class Fixture:
    """Two firms with audit rows, and an operator who belongs to only one."""

    def __init__(self) -> None:
        self.alpha = Tenant.objects.create(name="Alpha", slug="alpha")
        self.beta = Tenant.objects.create(name="Beta", slug="beta")
        for tenant, marker in ((self.alpha, ALPHA_MARKER), (self.beta, BETA_MARKER)):
            with tenant_context(tenant.id):
                Event.objects.create(
                    tenant=tenant,
                    action=AuditAction.EXPORT,
                    object_type="ClientCompany",
                    object_id=marker,
                )
        self.operator = _staff_user("op@alpha.example")
        Membership.objects.create(
            user=self.operator,
            tenant=self.alpha,
            role=TenantRole.OWNER,
        )


def _staff_user(email: str, *, superuser: bool = False) -> User:
    user = (
        User.objects.create_superuser(email=email)
        if superuser
        else User.objects.create_user(email=email, is_staff=True)
    )
    if not superuser:
        user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="audit",
                codename="view_event",
            ),
        )
    enrol_totp(user)
    return user


@pytest.fixture
def data() -> Fixture:
    return Fixture()


def _client_for(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _select(client: Client, tenant: Tenant, reason: str = "") -> HttpResponseBase:
    return client.post(
        SELECT_URL,
        {"tenant": str(tenant.pk), "reason": reason, "next": EVENT_CHANGELIST},
    )


# ------------------------------------------------------------------- authorization


def test_a_staff_user_cannot_select_a_firm_they_do_not_belong_to(
    data: Fixture,
) -> None:
    # Given an operator whose only membership is Alpha
    client = _client_for(data.operator)

    # When they submit BETA's id directly — which is how the value arrives, so
    # filtering the dropdown would not have prevented this
    response = _select(client, data.beta)

    # Then they are refused, nothing is stored in the session, and no audit event
    # claims the access happened
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert ADMIN_TENANT_SESSION_KEY not in client.session
    with tenant_context(data.beta.id):
        assert Event.objects.filter(action=AuditAction.TENANT_SELECTED).count() == 0


def test_a_staff_user_may_select_a_firm_they_belong_to(data: Fixture) -> None:
    # Given the same operator
    client = _client_for(data.operator)

    # When they select Alpha, where they hold an active membership
    response = _select(client, data.alpha)

    # Then it is accepted. Without this the refusal above would be satisfied by a
    # selector that refuses everything.
    assert response.status_code == HTTPStatus.FOUND
    assert client.session[ADMIN_TENANT_SESSION_KEY] == str(data.alpha.pk)


def test_a_revoked_membership_stops_working_on_the_next_request(
    data: Fixture,
) -> None:
    # Given an operator who has already selected their firm
    client = _client_for(data.operator)
    _select(client, data.alpha)
    assert client.get(EVENT_CHANGELIST).status_code == HTTPStatus.OK

    # When their membership is revoked
    Membership.objects.filter(user=data.operator).update(is_active=False)

    # Then the selection stops working immediately rather than at the next sign-in
    response = client.get(EVENT_CHANGELIST)
    assert ALPHA_MARKER not in response.content.decode()
    assert ADMIN_TENANT_SESSION_KEY not in client.session


# --------------------------------------------------------------------- break-glass


def test_a_superuser_needs_a_reason_to_reach_a_firm_they_do_not_belong_to(
    data: Fixture,
) -> None:
    # Given a platform superuser with no membership anywhere
    root = _staff_user("root@platform.example", superuser=True)
    client = _client_for(root)

    # When they try to select Beta with no reason given
    response = _select(client, data.beta, reason="")

    # Then it is refused: cross-tenant reach is break-glass, not a shortcut
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert ADMIN_TENANT_SESSION_KEY not in client.session


def test_break_glass_with_a_reason_needs_an_owner_grant_first(
    data: Fixture,
) -> None:
    # Given the same superuser — since AXR1-023 a stated reason alone is not
    # enough: the firm's owner must have approved a live support grant for this
    # exact account, or the selector refuses with no impersonation recorded.
    root = _staff_user("root@platform.example", superuser=True)
    client = _client_for(root)

    # When they state a reason but hold no grant
    refused = _select(client, data.beta, reason="Chamado #4711, suporte solicitado")
    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert ADMIN_TENANT_SESSION_KEY not in client.session

    # When Beta's owner approves the pending request for this account
    owner = _staff_user("owner@beta.example")
    Membership.objects.create(user=owner, tenant=data.beta, role=TenantRole.OWNER)

    grant = request_support_grant(
        root, data.beta.pk, purpose="Chamado #4711, suporte solicitado"
    )
    with tenant_context(data.beta.pk):
        approve_support_grant(
            owner,
            data.beta.pk,
            grant.pk,
            SupportApproval(scope="read_only", hours=2, client_ids=()),
        )

    # Then the same stated reason is granted AND an impersonation event names
    # actor, target and reason
    response = _select(client, data.beta, reason="Chamado #4711, suporte solicitado")
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(data.beta.id):
        impersonation = Event.objects.get(action=AuditAction.IMPERSONATION)
    assert impersonation.actor_id == root.pk
    assert impersonation.tenant_id == data.beta.pk
    assert impersonation.metadata["reason"] == "Chamado #4711, suporte solicitado"


def test_every_selection_is_audited_not_only_break_glass(data: Fixture) -> None:
    # Given an operator selecting their own firm — the ordinary, unremarkable case
    client = _client_for(data.operator)
    _select(client, data.alpha)

    # When the audit trail is read
    with tenant_context(data.alpha.id):
        selections = Event.objects.filter(action=AuditAction.TENANT_SELECTED)
        recorded = list(selections)

    # Then the ordinary case is recorded too. A platform operator opening a firm's
    # registry is processing personal data under LGPD, and an access-log URL is not an
    # adequate record of WHICH firm was opened.
    assert len(recorded) == 1
    assert recorded[0].actor_id == data.operator.pk
    assert recorded[0].metadata["break_glass"] is False
