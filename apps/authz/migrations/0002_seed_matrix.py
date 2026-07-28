"""Seed the published permission matrix: 16 capabilities x 6 roles = 96 grants.

Three roles in the matrix have no assignment path in this phase, and their absence is
deliberate rather than an unfinished edge:

* `platform_admin` is never written to `tenants.Membership`. `can()` short-circuits on
  `User.is_superuser` before the membership lookup, which covers the report's platform
  admin column without migrating a model that has already shipped.
* `client_owner` and `client_collaborator` are seed-only until the Phase 2 client
  portal exists. Nothing can hold them yet. They are seeded now so the stored matrix
  stays a faithful copy of the published one, and so the portal lands as a login path
  rather than as a second authorization design written under deadline.

Both tables are platform level and appear in `apps.core.rls.NON_TENANT_TABLES`, so no
tenant-scoped row is touched here and the migration needs no cross-tenant marker.

`update_or_create` rather than `create`: the operation must be safe to replay against a
database that already holds part of the matrix, which is what makes fixing a wrong cell
a re-run rather than a hand-written repair script.
"""

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.authz.matrix import MATRIX


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Write every capability and every grant, replacing any that drifted."""
    capability_model = apps.get_model("authz", "Capability")
    grant_model = apps.get_model("authz", "RoleGrant")
    for row in MATRIX:
        capability, _created = capability_model.objects.update_or_create(
            slug=row.slug,
            defaults={"label": row.label},
        )
        for role, level in row.grants().items():
            grant_model.objects.update_or_create(
                role=role,
                capability=capability,
                defaults={"level": level},
            )


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Remove the seeded capabilities, and their grants with them."""
    capability_model = apps.get_model("authz", "Capability")
    capability_model.objects.filter(slug__in=[row.slug for row in MATRIX]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("authz", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
