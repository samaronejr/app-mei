"""Seed `obligations.view_revenue_threshold_queue` and its six grants.

A new migration rather than an edit to `0002_seed_matrix`. Django will not re-run an
applied migration, so amending the old one fixes a fresh test database and leaves every
already-deployed environment on the previous matrix — green in CI, wrong in production.
That is the verify-by-effect failure this project has a decided constraint about.

The capability is scoped to this one slug rather than replaying the whole matrix. A full
replay would also be correct, since `0002`'s `update_or_create` is idempotent, but it
would overwrite any grant deliberately corrected in an environment since — and a
migration that silently reverts an operator's fix is worse than one that is narrow.
"""

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.authz.matrix import MATRIX

SLUG = "obligations.view_revenue_threshold_queue"


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Write the capability and all six of its grants, replacing any that drifted."""
    row = next(entry for entry in MATRIX if entry.slug == SLUG)
    capability_model = apps.get_model("authz", "Capability")
    grant_model = apps.get_model("authz", "RoleGrant")
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
    """Remove the capability, and its grants with it.

    Deliberate rather than `RunPython.noop`: reversing this migration must leave the
    stored matrix matching the published one at that revision. Leaving the row behind
    would make a rolled-back database claim a capability the report does not list, and
    the matrix meta-test compares the two.
    """
    capability_model = apps.get_model("authz", "Capability")
    capability_model.objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("authz", "0003_alter_capability_id_alter_rolegrant_id"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
