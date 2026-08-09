"""The portfolio scope may only ever narrow what the matrix already permits.

The first case is the important one. `FIRM_WIDE_ROLES` is a product rule sitting under
the published matrix, and the only way it can be wrong in a dangerous direction is by
naming a role the matrix does not grant `clients.view_all` — which would widen a
portfolio past its permission ceiling. Comparing the set against the seeded grants
makes that impossible to introduce silently.
"""

import itertools
from typing import Final

import pytest
from django.contrib.auth.models import AnonymousUser
from pytest_django.fixtures import DjangoAssertNumQueries

from apps.accounts.models import User
from apps.authz.models import Capability, GrantLevel, RoleGrant
from apps.authz.portfolio import (
    FIRM_WIDE_ROLES,
    VIEW_ALL,
    VIEW_ASSIGNED,
    PortfolioScope,
    portfolio_scope,
    visible_clients,
)
from apps.authz.services import (
    Actor,
    granted_levels,
    granted_levels_with_role,
    resolve_level,
    role_of,
)
from apps.authz.stash import PortalStash, portal_stash
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.tenants.models import CLIENT_ROLES, Membership, Tenant, TenantRole
from tests.ui.factories import Firm, assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def alpha() -> Firm:
    firm = make_firm("alpha-scope", client_count=3)
    assign(firm, firm.clients[0], firm.accountant)
    return firm


def test_no_firm_wide_role_exceeds_the_matrix() -> None:
    """The set may narrow the matrix. It must never reach past it."""
    capability = Capability.objects.get(slug=VIEW_ALL)
    permitted = set(
        RoleGrant.objects.filter(
            capability=capability,
            level=GrantLevel.FULL,
        ).values_list("role", flat=True),
    )
    assert permitted >= FIRM_WIDE_ROLES, (
        "a role here holds a firm-wide portfolio without holding clients.view_all"
    )


def test_the_set_is_a_strict_narrowing_and_not_a_copy() -> None:
    """If it equalled the matrix it would not be narrowing anything, and the staff
    accountant's assignment scope — which three acceptance criteria name — would be
    silently absent."""
    assert TenantRole.STAFF_ACCOUNTANT not in FIRM_WIDE_ROLES


def test_an_owner_works_the_whole_firm(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(alpha.owner) is PortfolioScope.ALL
        assert visible_clients(alpha.owner).count() == len(alpha.clients)


def test_a_staff_accountant_works_their_assignments(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(alpha.accountant) is PortfolioScope.ASSIGNED
        assert list(visible_clients(alpha.accountant)) == [alpha.clients[0]]


def test_an_anonymous_caller_gets_nothing(alpha: Firm) -> None:
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(AnonymousUser()) is PortfolioScope.NONE
        assert list(visible_clients(AnonymousUser())) == []


def test_an_account_with_no_membership_here_gets_nothing(alpha: Firm) -> None:
    outsider = User.objects.create_user(
        email="fora@example.com",
        password="irrelevant-here",  # noqa: S106
    )
    with tenant_context(alpha.tenant.id):
        assert portfolio_scope(outsider) is PortfolioScope.NONE
        assert list(visible_clients(outsider)) == []


def test_visible_clients_is_always_a_queryset_never_none(alpha: Firm) -> None:
    """Callers compose further filters onto it; None would be an AttributeError at
    the worst possible moment, and a list would silently drop the tenant filter."""
    with tenant_context(alpha.tenant.id):
        for actor in (alpha.owner, alpha.accountant, AnonymousUser()):
            result = visible_clients(actor)
            assert hasattr(result, "filter")


def test_the_orm_layer_scopes_on_its_own_without_help_from_the_policy(
    alpha: Firm,
) -> None:
    """Layer 1 must carry a tenant predicate of its own, not lean on layer 2.

    This test exists because of a real escape. `visible_clients` was changed to the
    unscoped base manager and the entire suite stayed green — row-level security
    contained it, exactly as designed, and every behavioural assertion in the project
    therefore passed while one of the two isolation layers was gone. Defence in depth
    is only depth if each layer is verified separately, so this reads the SQL rather
    than the rows.
    """
    with tenant_context(alpha.tenant.id):
        for actor in (alpha.owner, alpha.accountant):
            # The WHERE clause specifically. `tenant_id` is in the SELECT list of
            # every one of these querysets whether or not it is filtered on, so
            # searching the whole statement passes for the unscoped manager too —
            # which is how the first version of this test managed to be vacuous.
            _, _, predicate = str(visible_clients(actor).query).partition(" WHERE ")
            assert "tenant_id" in predicate, (
                "the portfolio queryset carries no tenant predicate; it is relying "
                "on row-level security to do layer 1's job"
            )


def test_another_firms_clients_are_never_visible(alpha: Firm) -> None:
    beta = make_firm("beta-scope", client_count=2)
    with tenant_context(alpha.tenant.id):
        visible = set(visible_clients(alpha.owner).values_list("pk", flat=True))
    assert visible.isdisjoint({client.pk for client in beta.clients})


def _branch_actors(alpha: Firm) -> tuple[User, User, PortalStash]:
    outsider = User.objects.create_user(email="outsider@scope.example.com")
    superuser = User.objects.create_superuser(
        email="superuser@scope.example.com",
        password=None,
    )
    stash = PortalStash(
        user_pk=alpha.owner.pk,
        tenant_id=alpha.tenant.pk,
        levels={
            VIEW_ASSIGNED: GrantLevel.FULL,
            VIEW_ALL: GrantLevel.NONE,
        },
    )
    return outsider, superuser, stash


def test_the_separate_resolution_query_count_is_explicit_on_every_branch(
    alpha: Firm,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    outsider, superuser, stash = _branch_actors(alpha)
    actions = (VIEW_ASSIGNED, VIEW_ALL)

    with tenant_context(alpha.tenant.pk):
        token = portal_stash.set(stash)
        try:
            with django_assert_num_queries(0):
                stashed = granted_levels(alpha.owner, actions)
        finally:
            portal_stash.reset(token)
        with django_assert_num_queries(1):
            anonymous = granted_levels(AnonymousUser(), actions)
        with django_assert_num_queries(1):
            elevated = granted_levels(superuser, actions)
        with django_assert_num_queries(2):
            missing = granted_levels(outsider, actions)
        with django_assert_num_queries(4):
            widest = granted_levels(alpha.owner, actions)
            separate_role = role_of(alpha.owner)

    assert stashed == {
        VIEW_ASSIGNED: GrantLevel.FULL,
        VIEW_ALL: GrantLevel.NONE,
    }
    assert set(anonymous.values()) == {GrantLevel.NONE}
    assert set(elevated.values()) == {GrantLevel.FULL}
    assert set(missing.values()) == {GrantLevel.NONE}
    assert set(widest.values()) == {GrantLevel.FULL}
    assert separate_role == TenantRole.OWNER


def test_role_and_levels_share_one_live_resolution_on_every_branch(
    alpha: Firm,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    outsider, superuser, stash = _branch_actors(alpha)
    Membership.objects.create(
        user=superuser,
        tenant=alpha.tenant,
        role=TenantRole.OWNER,
    )
    actions = (VIEW_ASSIGNED, VIEW_ALL)

    with tenant_context(alpha.tenant.pk):
        token = portal_stash.set(stash)
        try:
            with django_assert_num_queries(0):
                stashed = granted_levels_with_role(alpha.owner, actions)
        finally:
            portal_stash.reset(token)
        with django_assert_num_queries(1):
            anonymous = granted_levels_with_role(AnonymousUser(), actions)
        with django_assert_num_queries(2):
            elevated = granted_levels_with_role(superuser, actions)
        with django_assert_num_queries(2):
            missing = granted_levels_with_role(outsider, actions)
        with django_assert_num_queries(3):
            widest = granted_levels_with_role(alpha.owner, actions)

    assert stashed == (
        None,
        {VIEW_ASSIGNED: GrantLevel.FULL, VIEW_ALL: GrantLevel.NONE},
    )
    assert anonymous == (
        None,
        {VIEW_ASSIGNED: GrantLevel.NONE, VIEW_ALL: GrantLevel.NONE},
    )
    assert elevated == (
        TenantRole.OWNER,
        {VIEW_ASSIGNED: GrantLevel.FULL, VIEW_ALL: GrantLevel.FULL},
    )
    assert missing == (
        None,
        {VIEW_ASSIGNED: GrantLevel.NONE, VIEW_ALL: GrantLevel.NONE},
    )
    assert widest == (
        TenantRole.OWNER,
        {VIEW_ASSIGNED: GrantLevel.FULL, VIEW_ALL: GrantLevel.FULL},
    )


# ------------------------------------------------------------------- the truth table
#
# `portfolio_scope` resolves its two capabilities and role through
# `granted_levels_with_role`, in one live answer rather than by asking `can()` twice and
# then reading the role separately. That is a query optimisation and it must never
# become a second opinion — the same claim `granted_levels` itself carries one layer
# down, where `test_bulk_resolution_agrees_with_resolve_level` walks every capability
# against every role to prove the bulk resolver cannot drift from the single one. This
# is that argument at the scope layer, needed for a reason the layer below does not
# have: the old code SHORT-CIRCUITED. It never asked about
# `clients.view_all` unless `clients.view_assigned` had already answered yes, and the
# batched form asks about both at once. So the two can only be proved equivalent by
# enumerating the decision, not by reading it.
#
# The oracle below is deliberately the *old* shape — `resolve_level` once per
# capability, in the same order, with the same short-circuit — so it is independent of
# the resolver under test rather than a transcription of it.
#
# Seven grant levels matter rather than two, and that is the whole point of sweeping
# them. `can()` with no object answers True only for `full`: `limited` and `own` need an
# object and get `None` here, and the three channel levels are refusals by design. A
# batched form that compared truthiness, or treated any non-`none` level as a yes, would
# widen the portfolio for every role holding `view_all` at `limited` — and the shipped
# matrix would never show it, because no role holds either of these two at those levels
# today. Which is exactly why the sweep writes the levels rather than reading them.
ALL_LEVELS: Final[list[str]] = [choice.value for choice in GrantLevel]

# What the swept levels can produce for each role, exactly. A role outside
# FIRM_WIDE_ROLES can never reach ALL however wide its grants are — that is the
# narrowing this module exists to perform — and a client-side role can never reach
# anything, because `role_of` filters on the request's client and a client-side
# membership carries one while a firm-side context does not, so no membership matches
# and every capability resolves to NONE before the grants are ever consulted.
REACHABLE_SCOPES: Final[dict[str, set[PortfolioScope]]] = {
    TenantRole.OWNER: {
        PortfolioScope.NONE,
        PortfolioScope.ASSIGNED,
        PortfolioScope.ALL,
    },
    TenantRole.OPERATIONS_ADMIN: {
        PortfolioScope.NONE,
        PortfolioScope.ASSIGNED,
        PortfolioScope.ALL,
    },
    TenantRole.STAFF_ACCOUNTANT: {PortfolioScope.NONE, PortfolioScope.ASSIGNED},
    TenantRole.CLIENT_OWNER: {PortfolioScope.NONE},
    TenantRole.CLIENT_COLLABORATOR: {PortfolioScope.NONE},
}

# The scope each role resolves to under the matrix as actually seeded, asserted as
# literals. Without these the sweep could agree with an oracle that had drifted in the
# same direction as the code; with them, the two halves have to be wrong identically.
SHIPPED_SCOPES: Final[dict[str, PortfolioScope]] = {
    TenantRole.OWNER: PortfolioScope.ALL,
    TenantRole.OPERATIONS_ADMIN: PortfolioScope.ALL,
    TenantRole.STAFF_ACCOUNTANT: PortfolioScope.ASSIGNED,
    TenantRole.CLIENT_OWNER: PortfolioScope.NONE,
    TenantRole.CLIENT_COLLABORATOR: PortfolioScope.NONE,
}


@pytest.fixture
def bare() -> Tenant:
    """A firm with no clients and no accounts, so each case builds only its own."""
    return Tenant.objects.create(name="Tabela", slug="tabela-scope")


def _member(tenant: Tenant, role: str) -> User:
    """One account holding `role`, shaped the way the CHECK constraint demands.

    Mirrors `tests/authz/test_granted_levels.py`: a client-side role names the client
    it is held over and a firm-side role must not, so the parametrisation over every
    TenantRole has to supply the right shape rather than defaulting one.
    """
    user = User.objects.create_user(
        email=f"{role}@tabela-scope.example.com",
        password="irrelevant-here",  # noqa: S106
    )
    client = None
    if role in CLIENT_ROLES:
        with tenant_context(tenant.id):
            # ALL_OBJECTS_OK: fixture seeding for a portal membership, which by
            # definition needs the client row to exist first.
            client = ClientCompany.all_objects.create(
                tenant=tenant,
                legal_name=f"CLIENTE {role}",
                cnpj=f"1122233300{len(role):04d}",
                is_mei=True,
            )
    Membership.objects.create(user=user, tenant=tenant, role=role, client=client)
    return user


def _scope_one_capability_at_a_time(user: Actor) -> PortfolioScope:
    """The scope, resolved the way this module resolved it before the batching.

    `can(user, action)` with no object is exactly `resolve_level(...) == FULL` — the two
    object-resolved levels answer False against a `None` object and the three channel
    levels answer False outright — so this is the old decision, not a paraphrase of it.
    """
    if resolve_level(user, VIEW_ASSIGNED) != GrantLevel.FULL:
        return PortfolioScope.NONE
    if resolve_level(user, VIEW_ALL) != GrantLevel.FULL:
        return PortfolioScope.ASSIGNED
    role = role_of(user) if isinstance(user, User) else None
    return PortfolioScope.ALL if role in FIRM_WIDE_ROLES else PortfolioScope.ASSIGNED


def _set_level(role: str, slug: str, level: str) -> None:
    RoleGrant.objects.update_or_create(
        role=role,
        capability=Capability.objects.get(slug=slug),
        defaults={"level": level},
    )


@pytest.mark.parametrize("role", [choice.value for choice in TenantRole])
def test_the_scope_under_the_seeded_matrix_is_the_documented_one(
    bare: Tenant,
    role: str,
) -> None:
    """Pin the shipped answer per role as a literal, before anything is swept."""
    user = _member(bare, role)
    with tenant_context(bare.id):
        assert portfolio_scope(user) is SHIPPED_SCOPES[role]


@pytest.mark.parametrize("role", [choice.value for choice in TenantRole])
def test_the_batched_scope_agrees_for_every_level_pair(bare: Tenant, role: str) -> None:
    """Every (view_assigned, view_all) level pair, against the one-at-a-time answer."""
    user = _member(bare, role)
    pairs = list(itertools.product(ALL_LEVELS, ALL_LEVELS))
    assert len(pairs) == len(ALL_LEVELS) ** 2 == 49, (
        "the sweep is not covering the whole level square, so 'every capability "
        "combination' is a claim about a subset"
    )

    seen: set[PortfolioScope] = set()
    with tenant_context(bare.id):
        for assigned_level, all_level in pairs:
            _set_level(role, VIEW_ASSIGNED, assigned_level)
            _set_level(role, VIEW_ALL, all_level)
            expected = _scope_one_capability_at_a_time(user)
            actual = portfolio_scope(user)
            assert actual is expected, (
                f"{role} holding view_assigned={assigned_level!r} and "
                f"view_all={all_level!r} is scoped {actual} in one round trip but "
                f"{expected} when each capability is asked on its own"
            )
            seen.add(actual)

    assert seen == REACHABLE_SCOPES[role], (
        f"{role} reached {sorted(seen)} across the level square, not "
        f"{sorted(REACHABLE_SCOPES[role])}; either the sweep stopped exercising the "
        f"branches it is here to cover, or the narrowing rule changed"
    )
