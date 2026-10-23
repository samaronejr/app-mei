"""Seed the five regional MVP capability rows of plan A7.6 (AXRM-007).

Scoped to the slugs this migration introduces, for the reason 0006 records: a full
replay would overwrite grants an operator corrected since. The reverse removes only
these five capabilities and their grants, so the stored matrix matches the published
one at the previous revision.
"""

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.authz.matrix import MATRIX

SLUGS: tuple[str, ...] = (
    "fiscal.connections.manage",
    "fiscal.authority.manage",
    "fiscal.checks.run",
    "municipal.rules.manage",
    "municipal.rules.review",
)


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Write each regional capability and all six of its grants, repairing drift."""
    capability_model = apps.get_model("authz", "Capability")
    grant_model = apps.get_model("authz", "RoleGrant")
    for slug in SLUGS:
        row = next(entry for entry in MATRIX if entry.slug == slug)
        capability, _created = capability_model.objects.update_or_create(
            slug=row.slug, defaults={"label": row.label}
        )
        for role, level in row.grants().items():
            grant_model.objects.update_or_create(
                role=role, capability=capability, defaults={"level": level}
            )


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Remove the five regional capabilities, their role grants and user grants."""
    grants = apps.get_model("authz", "UserCapabilityGrant")
    # CROSS_TENANT_OK: rollback retires only grants for these five capabilities.
    grants.objects.filter(capability__slug__in=SLUGS).delete()
    apps.get_model("authz", "Capability").objects.filter(slug__in=SLUGS).delete()


class Migration(migrations.Migration):
    dependencies = [("authz", "0008_seed_account_capability")]
    operations = [migrations.RunPython(seed, unseed)]
