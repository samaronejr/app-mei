"""Seed the S8.1 R1 capability rows and their grants.

A new migration rather than an edit to `0002_seed_matrix`, for the reason 0004
records: Django will not re-run an applied migration, so amending the old one fixes
fresh databases and leaves every already-deployed environment on the previous
matrix — green in CI, wrong in production.

Thirteen rows, scoped to the slugs this migration introduces rather than replaying
the whole matrix. A full replay would also be idempotent, but it would overwrite
any grant deliberately corrected in an environment since — and a migration that
silently reverts an operator's fix is worse than one that is narrow.

All thirteen use FULL and NONE only, matching the R1 rule: `require_can` passes no
object, so an object-refined level would be an unconditional denial and a channel
level would never grant the action. Per-client narrowing is the views' and
services' job, through `visible_clients`/`load_visible_client`.
"""

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.authz.matrix import MATRIX

SLUGS: tuple[str, ...] = (
    "clients.import",
    "clients.manage",
    "playbooks.manage",
    "cycles.operate",
    "cycles.review",
    "actions.approve",
    "cycles.complete",
    "cycles.reopen",
    "bulk.run",
    "automations.manage",
    "evidence.export",
    "support.grant",
    "publications.acknowledge",
)


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor) -> None:
    """Write each R1 capability and all six of its grants, repairing drift."""
    capability_model = apps.get_model("authz", "Capability")
    grant_model = apps.get_model("authz", "RoleGrant")
    for slug in SLUGS:
        row = next(entry for entry in MATRIX if entry.slug == slug)
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
    """Remove the R1 capabilities, and their grants with them.

    Deliberate rather than `RunPython.noop`: reversing this migration must leave
    the stored matrix matching the published one at that revision, or the matrix
    meta-test would compare a rolled-back database against a table that no longer
    names these rows.
    """
    capability_model = apps.get_model("authz", "Capability")
    capability_model.objects.filter(slug__in=SLUGS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("authz", "0005_usercapabilitygrant"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
