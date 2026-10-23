"""Populated R1 rollback removes only R1 grants, retaining the audit history."""

import importlib
import logging

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.audit.services import ObjectRef, record_event
from apps.authz.models import Capability, GrantLevel, RoleGrant, UserCapabilityGrant
from apps.core.tenancy import tenant_context
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


def populated_round_trip() -> None:
    """Also executed against the lane database logged in as app_migrator."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        role = cursor.fetchone()[0]
    assert role in {"app_test", "app_migrator"}
    logger.info("Migration round trip effective role: %s", role)
    migration = importlib.import_module(
        "apps.authz.migrations.0006_seed_r1_capabilities"
    )
    # T22's later R1 capability and AXRM-007's regional rows (authz.0009) are also
    # reversed by the target below.
    regional = importlib.import_module(
        "apps.authz.migrations.0009_seed_regional_capabilities"
    )
    r1_slugs = (*migration.SLUGS, "tenants.account", *regional.SLUGS)
    historical = (
        MigrationExecutor(connection)
        .loader.project_state([("authz", "0005_usercapabilitygrant")])
        .apps
    )
    grant_fields = [
        field.attname
        for field in historical.get_model("authz", "UserCapabilityGrant")._meta.fields
    ]
    role_fields = [
        field.attname
        for field in historical.get_model("authz", "RoleGrant")._meta.fields
    ]
    tenants = [
        Tenant.objects.create(name=f"Rollback {n}", slug=f"rollback-{n}")
        for n in range(2)
    ]
    user = User.objects.create_user(email="rollback@example.com", password=None)
    for tenant in tenants:
        with tenant_context(tenant.pk):
            for revoked in (False, True):
                grant = UserCapabilityGrant.objects.create(
                    tenant=tenant,
                    user=user,
                    capability=Capability.objects.get(slug="actions.approve"),
                    level=GrantLevel.FULL,
                    revoked_at=timezone.now() if revoked else None,
                )
                record_event(
                    action=AuditAction.CAPABILITY_REVOKED
                    if revoked
                    else AuditAction.CAPABILITY_GRANTED,
                    tenant_id=tenant.pk,
                    actor=user,
                    obj=ObjectRef(type="user_capability_grant", id=str(grant.pk)),
                    metadata={"capability": "actions.approve", "user_id": str(user.pk)},
                )
            UserCapabilityGrant.objects.create(
                tenant=tenant,
                user=user,
                capability=Capability.objects.get(slug="clients.view_all"),
                level=GrantLevel.FULL,
            )
    non_r1 = list(Capability.objects.exclude(slug__in=r1_slugs).order_by("pk").values())
    non_r1_roles = [
        dict(row)
        for row in RoleGrant.objects.exclude(capability__slug__in=r1_slugs)
        .order_by("pk")
        .values()
    ]
    non_r1_grants = [
        dict(row)
        # ALL_OBJECTS_OK: migration authority inspects both tenants without a GUC.
        for row in UserCapabilityGrant.all_objects.exclude(
            capability__slug__in=r1_slugs
        )
        .order_by("pk")
        .values()
    ]
    # ALL_OBJECTS_OK: compare the append-only evidence across both migrated tenants.
    audit = list(Event.all_objects.order_by("pk").values())
    matrix = list(
        RoleGrant.objects.filter(capability__slug__in=r1_slugs)
        .order_by("capability__slug", "role")
        .values_list("capability__slug", "role", "level")
    )
    assert len(audit) == 4
    assert len(non_r1_grants) == 2
    try:
        call_command("migrate", "authz", "0005", interactive=False, verbosity=1)
        assert not Capability.objects.filter(slug__in=r1_slugs).exists()
        assert (
            # ALL_OBJECTS_OK: remove active/revoked R1 grants in both tenants.
            list(UserCapabilityGrant.all_objects.order_by("pk").values(*grant_fields))
            == [{key: row[key] for key in grant_fields} for row in non_r1_grants]
        )
        assert list(Capability.objects.order_by("pk").values()) == non_r1
        assert list(RoleGrant.objects.order_by("pk").values(*role_fields)) == [
            {key: row[key] for key in role_fields} for row in non_r1_roles
        ]
        # ALL_OBJECTS_OK: rollback may not erase immutable evidence.
        assert list(Event.all_objects.order_by("pk").values()) == audit
    finally:
        # Restore every dependent leaf too: reversing authz now also reverses
        # the protected approval schema, not just this app's capability rows.
        call_command("migrate", interactive=False, verbosity=1)
    assert (
        list(
            RoleGrant.objects.filter(capability__slug__in=r1_slugs)
            .order_by("capability__slug", "role")
            .values_list("capability__slug", "role", "level")
        )
        == matrix
    )
    assert (
        # ALL_OBJECTS_OK: forward must not resurrect authority or lose other grants.
        list(UserCapabilityGrant.all_objects.order_by("pk").values()) == non_r1_grants
    )
    # ALL_OBJECTS_OK: forward also retains the audit trail.
    assert list(Event.all_objects.order_by("pk").values()) == audit
    logger.info(
        "Reverse + forward: active/revoked R1 grants removed; "
        "non-R1 data and audit unchanged."
    )


@pytest.mark.django_db(transaction=True)
def test_populated_r1_migration_round_trip() -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        populated_round_trip()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
