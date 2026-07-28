"""Assignment scoping, and the tenant-crossing write that RLS alone does not stop.

The central case here is `test_a_cross_tenant_assignment_is_rejected_by_the_constraint`.
It is written with a raw cursor on purpose: through the ORM the row would simply be
filtered out and the test would pass while proving nothing. PostgreSQL exempts
referential-integrity checks from row security, so without the composite foreign key
that INSERT genuinely succeeds — the policy inspects the child row's `tenant_id`,
which is correct, and never looks at whose client the `client_id` names.
"""

from typing import Any

import pytest
import uuid6
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.clients.models import (
    AssignmentRole,
    ClientAssignment,
    ClientCompany,
    ClientTag,
    Tag,
)
from apps.clients.services import assign_client, unassign_client
from apps.core.tenancy import tenant_context
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

ASSIGNMENT_TABLE = ClientAssignment._meta.db_table


class Firm:
    """One accounting firm with a roster and a two-client portfolio."""

    def __init__(self, slug: str) -> None:
        self.tenant = Tenant.objects.create(name=slug.title(), slug=slug)
        self.owner = self._member(f"owner@{slug}.example.com", TenantRole.OWNER)
        self.accountant = self._member(
            f"staff@{slug}.example.com",
            TenantRole.STAFF_ACCOUNTANT,
        )
        self.assigned = self._client(f"{slug} Assigned Ltda", f"{slug.upper()[0]}A")
        self.unassigned = self._client(f"{slug} Unassigned Ltda", f"{slug.upper()[0]}B")

    def _member(self, email: str, role: str) -> User:
        user = User.objects.create_user(email=email, password="irrelevant-here")  # noqa: S106
        Membership.objects.create(user=user, tenant=self.tenant, role=role)
        return user

    def _client(self, legal_name: str, prefix: str) -> ClientCompany:
        with tenant_context(self.tenant.id):
            return ClientCompany.objects.create(
                tenant=self.tenant,
                legal_name=legal_name,
                cnpj=f"{prefix}{uuid6.uuid7().hex[:12].upper()}",
            )


@pytest.fixture
def alpha() -> Firm:
    return Firm("alpha")


@pytest.fixture
def beta() -> Firm:
    return Firm("beta")


def _raw_insert_assignment(*, tenant_id: Any, client_id: Any, user_id: Any) -> None:  # noqa: ANN401
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {ASSIGNMENT_TABLE} "  # noqa: S608
            "(id, tenant_id, client_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s, 'primary', now())",
            [uuid6.uuid7(), tenant_id, client_id, user_id],
        )


def test_a_staff_accountants_queryset_excludes_unassigned_clients(
    alpha: Firm,
) -> None:
    # Given a firm whose accountant holds one of its two clients
    assert_isolated_role()
    with tenant_context(alpha.tenant.id):
        assign_client(
            actor=alpha.owner,
            client=alpha.assigned,
            user=alpha.accountant,
        )

        # When the assignment lens is applied
        visible = list(ClientCompany.objects.assigned_to(alpha.accountant))

    # Then the firm's other client is not in it, even though it is the same tenant
    assert [client.pk for client in visible] == [alpha.assigned.pk]


def test_a_firm_owner_is_not_restricted_by_assignment(alpha: Firm) -> None:
    # Given an owner holding no assignments at all
    assert_isolated_role()
    with tenant_context(alpha.tenant.id):
        assert not ClientAssignment.objects.filter(user=alpha.owner).exists()

        # When the firm's registry is read the ordinary way
        everything = set(ClientCompany.objects.values_list("pk", flat=True))

    # Then both clients are there. Assignment is a lens the authorization layer may
    # choose to apply, never a second isolation filter baked into the manager.
    assert everything == {alpha.assigned.pk, alpha.unassigned.pk}


def test_the_same_account_cannot_be_assigned_to_one_client_twice(
    alpha: Firm,
) -> None:
    # Given an existing assignment
    assert_isolated_role()
    with tenant_context(alpha.tenant.id):
        assign_client(actor=alpha.owner, client=alpha.assigned, user=alpha.accountant)

        # When the same pair is assigned again
        # Then the unique constraint rejects it
        with (
            pytest.raises(
                IntegrityError, match="clientassignment_tenant_client_user_uniq"
            ),
            transaction.atomic(),
        ):
            assign_client(
                actor=alpha.owner,
                client=alpha.assigned,
                user=alpha.accountant,
                role=AssignmentRole.SUPPORT,
            )


def test_a_cross_tenant_assignment_is_rejected_by_the_constraint(
    alpha: Firm,
    beta: Firm,
) -> None:
    # Given two firms, and a raw INSERT that claims Alpha's tenant while naming
    # Beta's client — the shape row-level security cannot see, because the foreign-key
    # check that resolves client_id is documented to bypass row security
    assert_isolated_role()

    # When it is written through a raw cursor, inside Alpha's own context. The inner
    # atomic() is a savepoint so the rejected statement does not poison the
    # surrounding transaction before the assertion below can run.
    with (
        tenant_context(alpha.tenant.id),
        pytest.raises(IntegrityError) as raised,
        transaction.atomic(),
    ):
        _raw_insert_assignment(
            tenant_id=alpha.tenant.id,
            client_id=beta.assigned.id,
            user_id=alpha.accountant.id,
        )

    # Then the COMPOSITE key rejects it. Naming the constraint is the whole assertion:
    # the single-column client_id foreign key is satisfied by this row, so an error
    # from any other constraint would mean the hole is still open.
    assert "clientassignment_tenant_client_fk" in str(raised.value)


def test_a_cross_tenant_tag_link_is_rejected_by_the_constraint(
    alpha: Firm,
    beta: Firm,
) -> None:
    # Given a tag owned by Alpha and a client owned by Beta
    assert_isolated_role()
    with tenant_context(alpha.tenant.id):
        tag = Tag.objects.create(tenant=alpha.tenant, name="prioritario")

    # When Alpha tries to label Beta's client through a raw cursor
    with (
        tenant_context(alpha.tenant.id),
        pytest.raises(IntegrityError) as raised,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            f"INSERT INTO {ClientTag._meta.db_table} "  # noqa: S608
            "(id, tenant_id, client_id, tag_id, created_at) "
            "VALUES (%s, %s, %s, %s, now())",
            [uuid6.uuid7(), alpha.tenant.id, beta.assigned.id, tag.id],
        )

    # Then the composite key rejects it. Django's implicit join table would have had
    # no tenant column at all and could not have carried this constraint.
    assert "clienttag_tenant_client_fk" in str(raised.value)


def test_the_tag_join_table_is_an_explicit_tenant_scoped_model() -> None:
    # Given the many-to-many between tags and clients
    # When its through model is inspected
    through = Tag.clients.through

    # Then it is the explicit, tenant-scoped model rather than Django's generated
    # join table, which would carry no tenant column and therefore no policy
    assert through is ClientTag
    assert "tenant" in {field.name for field in through._meta.get_fields()}


def test_an_account_outside_the_firms_roster_cannot_be_assigned(
    alpha: Firm,
    beta: Firm,
) -> None:
    # Given an account belonging to another firm
    assert_isolated_role()

    # When it is assigned to this firm's client
    # Then the roster check refuses. accounts_user is global, so no composite key can
    # express this and the foreign-key check alone would have accepted the UUID.
    with tenant_context(alpha.tenant.id), pytest.raises(ValidationError, match="firm"):
        assign_client(
            actor=alpha.owner,
            client=alpha.assigned,
            user=beta.accountant,
        )


def test_assigning_and_unassigning_both_leave_an_audit_event(alpha: Firm) -> None:
    # Given a firm
    assert_isolated_role()

    # When an account is assigned and then removed
    with tenant_context(alpha.tenant.id):
        assignment = assign_client(
            actor=alpha.owner,
            client=alpha.assigned,
            user=alpha.accountant,
        )
        unassign_client(actor=alpha.owner, assignment=assignment)

        # Then both changes are on the record, attributed to the actor
        events = list(
            Event.objects.filter(action=AuditAction.ASSIGNMENT_CHANGED).order_by(
                "created_at",
            ),
        )

    assert [event.metadata["change"] for event in events] == [
        "assigned",
        "unassigned",
    ]
    assert {event.actor_id for event in events} == {alpha.owner.pk}
