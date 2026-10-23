"""AXRM-007 B1: reversing `authz.0009` removes only what that migration created.

A capability carrying one of the five regional slugs may exist before 0009 runs (an
operator row, or a later re-seed). The forward upserts it; the reverse must leave it,
its role grants (at their earlier levels) and every firm's user grants in place. A
capability 0009 did create is not 0009's to take away once a firm granted it to
someone, so that reverse refuses instead.
"""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.core.management import call_command
from django.db import connection

from apps.accounts.models import User
from apps.authz.models import Capability, GrantLevel, RoleGrant, UserCapabilityGrant
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant

REGIONAL = importlib.import_module(
    "apps.authz.migrations.0009_seed_regional_capabilities"
)
PREVIOUS = "0008_seed_account_capability"
PRE_EXISTING = "fiscal.checks.run"
CREATED_BY_SEED = "fiscal.connections.manage"
OPERATOR_LABEL = "Operator-defined federal checks"
OPERATOR_LEVELS = {"owner": GrantLevel.FULL, "staff_accountant": GrantLevel.NONE}


@contextmanager
def _as_migration_authority() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        call_command("migrate", interactive=False, verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


def _grants(slug: str) -> list[tuple[str, str]]:
    return sorted(
        RoleGrant.objects.filter(capability__slug=slug).values_list("role", "level")
    )


def _user_grants(slug: str) -> list[tuple[object, object, str, object]]:
    return sorted(
        # ALL_OBJECTS_OK: the migration authority compares every firm's grants.
        UserCapabilityGrant.all_objects.filter(capability__slug=slug).values_list(
            "pk", "tenant_id", "level", "revoked_at"
        ),
        key=str,
    )


def _two_firm_grants(slug: str) -> list[UserCapabilityGrant]:
    user = User.objects.create_user(email=f"{slug}@rollback.example", password=None)
    rows = []
    for n in range(2):
        tag = f"{slug.replace('.', '-')}-{n}"
        tenant = Tenant.objects.create(name=f"Regional {tag}", slug=tag)
        with tenant_context(tenant.pk):
            rows.append(
                UserCapabilityGrant.objects.create(
                    tenant=tenant,
                    user=user,
                    capability=Capability.objects.get(slug=slug),
                    level=GrantLevel.FULL,
                )
            )
    return rows


def populated_round_trip() -> None:
    """Also executed against the lane database logged in as app_migrator."""
    # Given 0009 not applied, and an operator row already holding one regional slug,
    # with its own label, two role grants and a grant in each of two firms
    call_command("migrate", "authz", PREVIOUS, interactive=False, verbosity=0)
    assert not Capability.objects.filter(slug__in=REGIONAL.SLUGS).exists()
    capability = Capability.objects.create(slug=PRE_EXISTING, label=OPERATOR_LABEL)
    for role, level in OPERATOR_LEVELS.items():
        RoleGrant.objects.create(role=role, capability=capability, level=level)
    _two_firm_grants(PRE_EXISTING)
    before_capability = list(Capability.objects.filter(slug=PRE_EXISTING).values())
    before_roles = _grants(PRE_EXISTING)
    before_users = _user_grants(PRE_EXISTING)
    assert len(before_users) == 2

    # When 0009 runs forward (repairing the row to the published matrix) and back
    call_command("migrate", "authz", interactive=False, verbosity=0)
    assert Capability.objects.filter(slug__in=REGIONAL.SLUGS).count() == len(
        REGIONAL.SLUGS
    )
    assert RoleGrant.objects.filter(capability__slug=PRE_EXISTING).count() == 6
    call_command("migrate", "authz", PREVIOUS, interactive=False, verbosity=0)

    # Then the operator row, its two grants at their earlier levels and both firms'
    # grants survive, and only the four capabilities 0009 created are gone
    assert list(Capability.objects.filter(slug=PRE_EXISTING).values()) == (
        before_capability
    )
    assert _grants(PRE_EXISTING) == before_roles
    assert _user_grants(PRE_EXISTING) == before_users
    assert set(
        Capability.objects.filter(slug__in=REGIONAL.SLUGS).values_list(
            "slug", flat=True
        )
    ) == {PRE_EXISTING}


def referenced_reverse_refuses() -> None:
    """A capability 0009 created and a firm then granted: the reverse refuses."""
    call_command("migrate", "authz", interactive=False, verbosity=0)
    granted = _two_firm_grants(CREATED_BY_SEED)
    before = _user_grants(CREATED_BY_SEED)

    with pytest.raises(Exception, match="forward repair"):
        call_command("migrate", "authz", PREVIOUS, interactive=False, verbosity=0)

    assert _user_grants(CREATED_BY_SEED) == before
    assert len(before) == len(granted)
    assert Capability.objects.filter(slug__in=REGIONAL.SLUGS).count() == len(
        REGIONAL.SLUGS
    )


@pytest.mark.django_db(transaction=True)
def test_the_reverse_keeps_a_capability_that_existed_before_the_seed() -> None:
    with _as_migration_authority():
        populated_round_trip()


@pytest.mark.django_db(transaction=True)
def test_the_reverse_refuses_while_a_firm_granted_a_seeded_capability() -> None:
    with _as_migration_authority():
        referenced_reverse_refuses()
