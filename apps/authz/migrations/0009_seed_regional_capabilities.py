"""Seed the five regional MVP capability rows of plan A7.6 (AXRM-007).

Scoped to the slugs this migration introduces, for the reason 0006 records: a full
replay would overwrite grants an operator corrected since.

The upsert cannot tell afterwards which rows it created and which it found, so the
forward records each capability and role grant it created, and the earlier label or
level of each one it changed, in `authz_capabilityseedrow` (the `fiscal.0003` ledger
pattern). The reverse deletes only what it created and puts back what it changed, so a
capability that held one of these slugs before 0009, its role grants and every firm's
user grants survive. A capability 0009 created that a firm has since granted to
someone is not this migration's to remove: that reverse refuses (forward repair).
"""

# CROSS_TENANT_OK: the reverse runs one read-only existence check on
# authz_usercapabilitygrant across every firm, because a grant in any firm must refuse
# the delete; no tenant row is read beyond that boolean or written.

from typing import Final

from django.apps import registry as app_registry
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

import apps.core.identifiers
from apps.authz.matrix import MATRIX

SEED: Final = "authz.0009_seed_regional_capabilities"
SLUGS: tuple[str, ...] = (
    "fiscal.connections.manage",
    "fiscal.authority.manage",
    "fiscal.checks.run",
    "municipal.rules.manage",
    "municipal.rules.review",
)
CAPABILITY_ROW: Final = ""


def _record(
    ledger: type[models.Model], slug: str, role: str, *, created: bool, previous: str
) -> None:
    """Keep the FIRST observation: a re-run must not relabel a found row as created."""
    ledger._default_manager.get_or_create(
        seed=SEED,
        capability_slug=slug,
        role=role,
        defaults={"created": created, "previous": previous},
    )


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Write each regional capability and its six grants, ledgering every change."""
    capability_model = apps.get_model("authz", "Capability")
    grant_model = apps.get_model("authz", "RoleGrant")
    ledger = apps.get_model("authz", "CapabilitySeedRow")
    for slug in SLUGS:
        row = next(entry for entry in MATRIX if entry.slug == slug)
        capability = capability_model.objects.filter(slug=slug).first()
        if capability is None:
            capability = capability_model.objects.create(slug=slug, label=row.label)
            _record(ledger, slug, CAPABILITY_ROW, created=True, previous="")
        elif capability.label != row.label:
            _record(
                ledger, slug, CAPABILITY_ROW, created=False, previous=capability.label
            )
            capability.label = row.label
            capability.save(update_fields=["label"])
        for role, level in row.grants().items():
            grant = grant_model.objects.filter(role=role, capability=capability).first()
            if grant is None:
                grant_model.objects.create(
                    role=role, capability=capability, level=level
                )
                _record(ledger, slug, role, created=True, previous="")
            elif grant.level != level:
                _record(ledger, slug, role, created=False, previous=grant.level)
                grant.level = level
                grant.save(update_fields=["level"])


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Refuse a granted created capability; otherwise undo exactly the ledgered work."""
    capability_model = apps.get_model("authz", "Capability")
    grant_model = apps.get_model("authz", "RoleGrant")
    user_grant_model = apps.get_model("authz", "UserCapabilityGrant")
    rows = list(apps.get_model("authz", "CapabilitySeedRow").objects.filter(seed=SEED))
    created = {
        row.capability_slug
        for row in rows
        if row.role == CAPABILITY_ROW and row.created
    }
    if user_grant_model._base_manager.filter(capability__slug__in=created).exists():
        message = (
            "authz.0009 cannot be reversed: a firm granted a capability this migration "
            "created. Recovery is forward repair (keep the capability)."
        )
        raise IrreversibleError(message)
    for row in rows:
        if row.capability_slug in created:
            continue
        if row.role == CAPABILITY_ROW:
            capability_model.objects.filter(slug=row.capability_slug).update(
                label=row.previous
            )
            continue
        grants = grant_model.objects.filter(
            role=row.role, capability__slug=row.capability_slug
        )
        if row.created:
            grants.delete()
        else:
            grants.update(level=row.previous)
    capability_model.objects.filter(slug__in=created).delete()


class Migration(migrations.Migration):
    dependencies = [("authz", "0008_seed_account_capability")]
    operations = [
        migrations.CreateModel(
            name="CapabilitySeedRow",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=apps.core.identifiers.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("seed", models.CharField(max_length=100, verbose_name="seed")),
                (
                    "capability_slug",
                    models.CharField(max_length=64, verbose_name="capability slug"),
                ),
                (
                    "role",
                    models.CharField(blank=True, max_length=32, verbose_name="role"),
                ),
                ("created", models.BooleanField(verbose_name="created by the seed")),
                (
                    "previous",
                    models.CharField(
                        blank=True, max_length=128, verbose_name="previous value"
                    ),
                ),
            ],
            options={
                "verbose_name": "capability seed row",
                "verbose_name_plural": "capability seed rows",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("seed", "capability_slug", "role"),
                        name="capabilityseedrow_seed_slug_role_uniq",
                    )
                ],
            },
        ),
        # A replayed migration's new table is owned by its creator, so restate the
        # runtime contract (same reason as automation.0001 and integrations.0001).
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON authz_capabilityseedrow "
            "TO app_runtime;",
            "REVOKE ALL ON authz_capabilityseedrow FROM app_runtime;",
        ),
        migrations.RunPython(seed, unseed),
    ]
