"""What the admin changelist actually renders, which is where the design nearly failed.

Two independent traps live here, and both produce an empty page rather than an error:

1. **The queryset is lazy.** Wrapping `ModelAdmin.get_queryset()` in `tenant_context`
   leaves it evaluated during template rendering, after the block has exited and both
   isolation layers are gone. The page renders empty and nothing raises.
2. **"Shows only the selected firm's rows" is trivially true of zero rows.** An
   acceptance criterion phrased that way is *satisfied* by a completely broken
   selector, which is why every assertion below requires a positive count.

So the tests assert against the rendered HTML, not against a queryset, and they assert
`> 0` for the selected firm before asserting `0` for the other.
"""

from http import HTTPStatus

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)

SELECT_URL = "/admin/selecionar-empresa/"
EVENT_CHANGELIST = "/admin/audit/event/"
ALPHA_MARKER = "alpha-only-marker"
BETA_MARKER = "beta-only-marker"
ROWS_PER_TENANT = 3


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.admin.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


class Fixture:
    """An operator who belongs to BOTH firms, so the contrast is about the selector."""

    def __init__(self) -> None:
        self.alpha = Tenant.objects.create(name="Alpha", slug="alpha")
        self.beta = Tenant.objects.create(name="Beta", slug="beta")
        for tenant, marker in ((self.alpha, ALPHA_MARKER), (self.beta, BETA_MARKER)):
            with tenant_context(tenant.id):
                for index in range(ROWS_PER_TENANT):
                    Event.objects.create(
                        tenant=tenant,
                        action=AuditAction.EXPORT,
                        object_type="ClientCompany",
                        object_id=f"{marker}-{index}",
                    )
        self.operator = User.objects.create_user(
            email="op@platform.example",
            is_staff=True,
        )
        self.operator.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="audit",
                codename="view_event",
            ),
        )
        enrol_totp(self.operator)
        for tenant in (self.alpha, self.beta):
            Membership.objects.create(
                user=self.operator,
                tenant=tenant,
                role=TenantRole.OWNER,
            )


@pytest.fixture
def data() -> Fixture:
    return Fixture()


@pytest.fixture
def client_for(data: Fixture) -> Client:
    client = Client()
    client.force_login(data.operator)
    return client


def _select(client: Client, tenant: Tenant) -> None:
    response = client.post(
        SELECT_URL,
        {"tenant": str(tenant.pk), "next": EVENT_CHANGELIST},
    )
    assert response.status_code == HTTPStatus.FOUND, "selection was refused"


def _rendered(client: Client) -> str:
    response = client.get(EVENT_CHANGELIST)
    assert response.status_code == HTTPStatus.OK
    return response.content.decode()


def test_the_changelist_shows_the_selected_firms_rows_and_no_others(
    data: Fixture,
    client_for: Client,
) -> None:
    # Given rows seeded for both firms and Alpha selected
    _select(client_for, data.alpha)

    # When the changelist renders
    body = _rendered(client_for)

    # Then Alpha's rows are present — strictly more than zero, so a broken selector
    # producing an empty page FAILS here rather than passing...
    assert body.count(ALPHA_MARKER) >= ROWS_PER_TENANT
    assert body.count(ALPHA_MARKER) > 0

    # ...and not one of Beta's rows appears
    assert BETA_MARKER not in body


def test_switching_the_selector_changes_the_visible_set(
    data: Fixture,
    client_for: Client,
) -> None:
    # Given Alpha selected
    _select(client_for, data.alpha)
    assert ALPHA_MARKER in _rendered(client_for)

    # When the operator switches to Beta
    _select(client_for, data.beta)
    body = _rendered(client_for)

    # Then the set flips completely, in both directions
    assert body.count(BETA_MARKER) >= ROWS_PER_TENANT
    assert ALPHA_MARKER not in body


def test_with_no_firm_selected_the_changelist_says_so_explicitly(
    client_for: Client,
) -> None:
    # Given a session that has selected nothing
    # When the changelist is requested
    response = client_for.get(EVENT_CHANGELIST)
    body = response.content.decode()

    # Then it renders an explicit selection prompt rather than an empty table, which
    # an operator would read as "this firm has no data"
    assert response.status_code == HTTPStatus.OK
    assert "Selecione uma empresa" in body
    assert ALPHA_MARKER not in body
    assert BETA_MARKER not in body


def test_the_rows_survive_template_rendering_not_just_the_queryset(
    data: Fixture,
    client_for: Client,
) -> None:
    # Given Alpha selected
    _select(client_for, data.alpha)

    # When the FULL response body is inspected, rather than a queryset the test
    # evaluated itself inside a context of its own making
    body = _rendered(client_for)

    # Then the rows are in the HTML. The changelist queryset is lazy and evaluates
    # during rendering, so this is the assertion that would fail if the tenant context
    # were established around get_queryset() alone.
    assert body.count(ALPHA_MARKER) >= ROWS_PER_TENANT


def test_the_audit_changelist_is_read_only(data: Fixture, client_for: Client) -> None:
    # Given a selected firm and one of its events
    _select(client_for, data.alpha)
    with tenant_context(data.alpha.id):
        event = Event.objects.filter(object_id__startswith=ALPHA_MARKER).first()
    assert event is not None

    # When an edit is POSTed to the admin change form
    response = client_for.post(
        f"/admin/audit/event/{event.pk}/change/",
        {"action": AuditAction.DAS_CONFIRMED, "object_type": "x", "object_id": "x"},
    )

    # Then it is refused. The database rejects UPDATE on this table for every role
    # including its owner, so an editable form would only ever produce an exception.
    assert response.status_code in {HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND}
    with tenant_context(data.alpha.id):
        assert Event.objects.get(pk=event.pk).action == AuditAction.EXPORT


def test_the_admin_is_not_reachable_on_a_firms_subdomain(
    data: Fixture,
    client_for: Client,
) -> None:
    # Given a signed-in operator who is a member of Alpha
    # When they reach for the console on Alpha's own subdomain
    response = client_for.get("/admin/", headers={"host": "alpha.localhost"})

    # Then it is not there. Served from a firm's subdomain the console would inherit
    # that firm's cookie scope and blur the boundary the tenancy model rests on. 404
    # rather than 403: its existence is not a fact that subdomain needs confirmed.
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert data.alpha.slug == "alpha"


def test_the_admin_is_reachable_on_the_platform_host(client_for: Client) -> None:
    # Given the same session on the platform hostname
    # When the console is requested
    response = client_for.get("/admin/")

    # Then it is served. Without this the 404 above would be satisfied by an admin
    # that is unreachable everywhere.
    assert response.status_code == HTTPStatus.OK
