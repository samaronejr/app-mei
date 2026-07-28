"""Why the audit trail is two tables, asserted so the reason cannot be forgotten.

`Event` is tenant-scoped, so its policy carries a `WITH CHECK` half. At login the GUC
is still empty, the predicate evaluates to NULL, and the INSERT is **rejected**. A
single-table design would therefore have made the product's own login flow impossible
while reading as correct.

The first case below asserts that rejection directly. Without it, someone could later
"simplify" the design by folding `PlatformEvent` back into `Event`, watch the unit
tests pass, and discover the consequence in production at the login page.
"""

from http import HTTPStatus

import psycopg
import pytest
from django.db import Error as DatabaseError
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event, PlatformEvent
from apps.audit.services import ObjectRef, Origin, record_event, record_platform_event
from apps.core.rls import is_exempt_from_tenant_policy
from apps.core.tenancy import MissingTenantContext, tenant_context
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from tests.isolation.rolecheck import assert_isolated_role
from tests.support import enrol_totp, sign_in

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = "correct-horse-battery-staple"  # noqa: S105


@pytest.fixture(autouse=True)
def _urls(settings: SettingsWrapper) -> None:
    settings.ROOT_URLCONF = "tests.accounts.urls"
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Alpha", slug="alpha")


def _insert_event_with_empty_guc(tenant_id: str) -> None:
    """Insert straight into audit_event with the GUC explicitly empty.

    The GUC is set INSIDE the transaction because `set_config(..., true)` is
    transaction-local — issued outside, it would be discarded before the insert ran
    and the test would prove nothing about the empty-string case.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', '', true)")
        cursor.execute(
            "INSERT INTO audit_event "
            "(id, tenant_id, action, object_type, object_id, metadata, created_at) "
            "VALUES (gen_random_uuid(), %s, 'export', '', '', '{}', now())",
            [tenant_id],
        )


def test_an_event_insert_with_an_empty_tenant_guc_is_rejected(tenant: Tenant) -> None:
    # Given no resolved tenant — the state every request is in at the login page
    assert_isolated_role()

    # When a tenant-scoped audit row is inserted anyway
    with pytest.raises(DatabaseError) as caught:
        _insert_event_with_empty_guc(str(tenant.id))

    # Then the database refuses it. THIS is why PlatformEvent exists; folding the two
    # tables back together would make login impossible.
    assert isinstance(caught.value.__cause__, psycopg.errors.InsufficientPrivilege)
    assert "row-level security" in str(caught.value)


def test_a_platform_event_insert_with_an_empty_tenant_guc_succeeds() -> None:
    # Given the same no-tenant state
    assert_isolated_role()
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id', '', true)")

    # When a platform-level audit row is inserted
    event = record_platform_event(
        action=AuditAction.LOGIN_FAILED,
        origin=Origin(subject="quem@sabe.example"),
    )

    # Then it lands. The contrast with the case above IS the design.
    assert PlatformEvent.objects.filter(pk=event.pk).count() == 1


def test_platform_event_is_allow_listed_and_event_is_not() -> None:
    # Given the row-level-security allow-list
    # When both audit tables are classified
    # Then only the platform table is exempt, and the tenant table is policed
    assert is_exempt_from_tenant_policy("audit_platformevent")
    assert not is_exempt_from_tenant_policy("audit_event")


def test_a_tenant_attributable_action_writes_exactly_one_event(tenant: Tenant) -> None:
    # Given a resolved tenant context
    assert_isolated_role()

    # When a tenant-attributable action is recorded
    with tenant_context(tenant.id):
        record_event(action=AuditAction.EXPORT, obj=ObjectRef(type="ClientCompany"))
        # Read inside the context: outside it, RLS correctly returns nothing
        count = Event.objects.count()

    # Then exactly one row exists
    assert count == 1


def test_record_event_refuses_to_write_without_a_tenant() -> None:
    # Given no tenant context at all
    assert_isolated_role()

    # When a tenant-attributable action is recorded
    # Then it fails loudly rather than writing an unattributable row
    with pytest.raises(MissingTenantContext):
        record_event(action=AuditAction.EXPORT)


def test_record_event_enters_context_when_given_a_tenant_explicitly(
    tenant: Tenant,
) -> None:
    # Given no ambient context, but an explicitly named tenant — the shape used right
    # after authentication, when the membership is known but the request is not scoped
    assert_isolated_role()

    # When the event is recorded
    record_event(action=AuditAction.ROLE_CHANGED, tenant_id=tenant.id)

    # Then it lands under that tenant
    with tenant_context(tenant.id):
        assert Event.objects.filter(action=AuditAction.ROLE_CHANGED).count() == 1


def test_logging_in_writes_one_platform_event_and_no_tenant_event(
    tenant: Tenant,
) -> None:
    # Given a firm-side user
    user = User.objects.create_user(email="owner@alpha.example", password=PASSWORD)
    Membership.objects.create(user=user, tenant=tenant, role=TenantRole.OWNER)
    enrol_totp(user)

    # When they sign in
    sign_in(user, PASSWORD, with_mfa=True)

    # Then the identity event is recorded at platform level, and nothing was written
    # to the tenant-scoped table — which could not have accepted it anyway
    logins = PlatformEvent.objects.filter(action=AuditAction.LOGIN_SUCCEEDED)
    assert logins.count() == 1
    assert logins.get().actor_id == user.pk
    with tenant_context(tenant.id):
        assert Event.objects.count() == 0


def test_a_failed_login_is_recorded_with_the_submitted_address() -> None:
    # Given no account for the submitted address
    # When a sign-in is attempted
    Client().post(
        reverse("account_login"),
        {"login": "ninguem@alpha.example", "password": "errada"},
    )

    # Then the attempt is on record. A failed login has no tenant by definition, so a
    # tenant-scoped audit table could never have held this row.
    failures = PlatformEvent.objects.filter(action=AuditAction.LOGIN_FAILED)
    assert failures.count() == 1
    assert failures.get().subject == "ninguem@alpha.example"
    assert failures.get().tenant_id is None


def test_invite_issuance_and_acceptance_are_recorded_at_platform_level(
    tenant: Tenant,
) -> None:
    # Given a firm owner who invites a colleague
    owner = User.objects.create_user(email="owner@alpha.example", password=PASSWORD)
    Membership.objects.create(user=owner, tenant=tenant, role=TenantRole.OWNER)
    invite, raw_token = Invite.issue(
        tenant=tenant,
        email="novo@alpha.example",
        role=TenantRole.STAFF_ACCOUNTANT,
    )
    record_platform_event(
        action=AuditAction.INVITE_ISSUED,
        origin=Origin(actor=owner, subject=invite.email),
        tenant_id=tenant.pk,
    )

    # When the invitee redeems it through the real acceptance route
    response = Client().post(
        f"/convites/aceitar/{raw_token}/",
        {"full_name": "Nova", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert response.status_code == HTTPStatus.FOUND

    # Then both halves of the invitation's life are on the platform stream, where
    # acceptance — which happens with no tenant resolved — can actually be written
    issued = PlatformEvent.objects.filter(action=AuditAction.INVITE_ISSUED)
    accepted = PlatformEvent.objects.filter(action=AuditAction.INVITE_ACCEPTED)
    assert issued.count() == 1
    assert accepted.count() == 1
    assert accepted.get().tenant_id == tenant.pk
    assert accepted.get().subject == "novo@alpha.example"
