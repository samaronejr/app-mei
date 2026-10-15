"""Owner-only account lifecycle and cost visibility, separate from plan operations."""

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.authz.matrix import MATRIX

SLUGS = ("tenants.account",)


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    capability_model = apps.get_model("authz", "Capability")
    grant_model = apps.get_model("authz", "RoleGrant")
    for row in MATRIX:
        if row.slug not in SLUGS:
            continue
        capability, _created = capability_model.objects.update_or_create(
            slug=row.slug, defaults={"label": row.label}
        )
        for role, level in row.grants().items():
            grant_model.objects.update_or_create(
                role=role, capability=capability, defaults={"level": level}
            )


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    grants = apps.get_model("authz", "UserCapabilityGrant")
    # CROSS_TENANT_OK: retire only this capability's grants on feature rollback.
    grants.objects.filter(capability__slug__in=SLUGS).delete()
    apps.get_model("authz", "Capability").objects.filter(slug__in=SLUGS).delete()


class Migration(migrations.Migration):
    dependencies = [("authz", "0007_grant_expiry_revision")]
    operations = [migrations.RunPython(seed, unseed)]
