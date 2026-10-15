"""Open the manual billing account every firm created before 0011 is missing.

# CROSS_TENANT_OK: app_migrator holds BYPASSRLS and this migration exists to give
# EVERY firm the row `create_firm` opens at onboarding. Each insert names its tenant.

The row is exactly the one onboarding writes with no band: provider manual, status
active, version 1. A firm that already holds an account keeps it untouched, so the
run is idempotent, and the pass refuses to finish while any firm still lacks one.
The reverse is a no-op: a backfilled row cannot be told apart from one an operator
has since relied on, and 0011's own reverse drops the table.
"""

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def backfill(
    apps: app_registry.Apps,
    _editor: BaseDatabaseSchemaEditor | None,
) -> None:
    """Give each firm without a billing account the onboarding default."""
    tenant_model = apps.get_model("tenants", "Tenant")
    account_model = apps.get_model("tenants", "BillingAccount")
    opened = set(account_model.objects.values_list("tenant_id", flat=True))
    missing = tenant_model.objects.exclude(pk__in=opened).order_by("slug")
    account_model.objects.bulk_create(
        account_model(tenant_id=tenant.pk) for tenant in missing
    )
    still_missing = tenant_model.objects.exclude(
        pk__in=account_model.objects.values("tenant_id")
    ).count()
    if still_missing:
        msg = f"{still_missing} firms still lack a billing account after the backfill."
        raise AssertionError(msg)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0013_support_grant_window_immutable"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
