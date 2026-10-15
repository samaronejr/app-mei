"""Backfill one TaxProfile and one Engagement per client, per tenant.

# CROSS_TENANT_OK: app_migrator holds BYPASSRLS and this migration's whole purpose is
# deriving rows for EVERY firm's portfolio. The scope is held explicit by iterating
# tenants and filtering every query on tenant_id — no statement runs across tenants
# without the tenant column named in it.

S12.1's mapping, verbatim:

* `is_mei=true`  -> TaxProfile(regime=mei, mei_category, valid_from=opened_on or
  created_at::date, provenance='backfill_v1', confidence='asserted_by_legacy_flag').
* `is_mei=false` -> TaxProfile(regime=unknown, valid_from=created_at::date, same
  provenance and confidence). The old schema could not say what regime a non-MEI
  client was under, so the backfill records "unknown" rather than guessing — staff
  set it, and the unknown period stays honestly unknown.
* every client   -> Engagement(service_kind='mei_mensal' if MEI else
  'documentos_mensal', starts_on=created_at::date, responsible=first assignment or
  null).

Two rules protect the run itself. It is **idempotent**: a client already carrying a
backfill_v1 profile or engagement is skipped, so a re-run creates nothing. And it is
**asserted**: the per-tenant totals of backfill_v1 rows must equal the client count
after the pass — a silent partial backfill (the failure mode BYPASSRLS exists to
prevent) raises instead of reporting success over missing rows.

No history is invented before `valid_from`: the earliest claim's start is the
registry's own date, and anything earlier reads as unknown — never as an inferred
regime.
"""

from typing import Any

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

BACKFILL_PROVENANCE = "backfill_v1"
LEGACY_CONFIDENCE = "asserted_by_legacy_flag"
REGIME_MEI = "mei"
REGIME_UNKNOWN = "unknown"
PACK_MEI = "mei_mensal"
PACK_DOCUMENTS = "documentos_mensal"


def backfill(
    apps: app_registry.Apps,
    _editor: BaseDatabaseSchemaEditor | None,
) -> None:
    """Create the derived rows for every client, one tenant at a time."""
    tenant_model = apps.get_model("tenants", "Tenant")
    client_model = apps.get_model("clients", "ClientCompany")
    assignment_model = apps.get_model("clients", "ClientAssignment")
    profile_model = apps.get_model("clients", "TaxProfile")
    engagement_model = apps.get_model("clients", "Engagement")

    for tenant in tenant_model.objects.order_by("slug"):
        clients = list(client_model.objects.filter(tenant_id=tenant.id))
        if not clients:
            continue

        # The first assignment is the engagement's responsible when one exists.
        # Ordered by creation then id so "first" is deterministic, not row order.
        responsible_by_client: dict[Any, Any] = {}
        assignments = assignment_model.objects.filter(tenant_id=tenant.id).order_by(
            "created_at", "pk"
        )
        for assignment in assignments:
            responsible_by_client.setdefault(assignment.client_id, assignment.user_id)

        profiled = set(
            profile_model.objects.filter(
                tenant_id=tenant.id, provenance=BACKFILL_PROVENANCE
            ).values_list("client_id", flat=True)
        )
        engaged = set(
            engagement_model.objects.filter(
                tenant_id=tenant.id, provenance=BACKFILL_PROVENANCE
            ).values_list("client_id", flat=True)
        )

        new_profiles = [
            profile_model(
                tenant_id=tenant.id,
                client_id=client.id,
                regime=REGIME_MEI if client.is_mei else REGIME_UNKNOWN,
                # The category means something only while the regime is MEI; on an
                # unknown claim it would assert knowledge the flag never held.
                mei_category=client.mei_category if client.is_mei else None,
                valid_from=(
                    client.opened_on or client.created_at.date()
                    if client.is_mei
                    else client.created_at.date()
                ),
                provenance=BACKFILL_PROVENANCE,
                confidence=LEGACY_CONFIDENCE,
            )
            for client in clients
            if client.id not in profiled
        ]
        new_engagements = [
            engagement_model(
                tenant_id=tenant.id,
                client_id=client.id,
                service_kind=PACK_MEI if client.is_mei else PACK_DOCUMENTS,
                starts_on=client.created_at.date(),
                responsible_id=responsible_by_client.get(client.id),
                provenance=BACKFILL_PROVENANCE,
            )
            for client in clients
            if client.id not in engaged
        ]
        profile_model.objects.bulk_create(new_profiles)
        engagement_model.objects.bulk_create(new_engagements)

        # The count assertion is on the TOTALS, not on what this run inserted: a
        # re-run legitimately creates nothing, and asserting "created == clients"
        # would make an idempotent re-run look like a failure.
        profile_total = profile_model.objects.filter(
            tenant_id=tenant.id, provenance=BACKFILL_PROVENANCE
        ).count()
        engagement_total = engagement_model.objects.filter(
            tenant_id=tenant.id, provenance=BACKFILL_PROVENANCE
        ).count()
        if profile_total != len(clients) or engagement_total != len(clients):
            msg = (
                f"backfill_v1 count mismatch for tenant {tenant.id}: "
                f"{profile_total} profiles and {engagement_total} engagements for "
                f"{len(clients)} clients. Refusing to report a partial backfill."
            )
            raise AssertionError(msg)


def unbackfill(
    apps: app_registry.Apps,
    _editor: BaseDatabaseSchemaEditor | None,
) -> None:
    """Remove only what this migration could have written — the marked rows."""
    profile_model = apps.get_model("clients", "TaxProfile")
    engagement_model = apps.get_model("clients", "Engagement")
    # CROSS_TENANT_OK: the reverse of a cross-tenant backfill is itself cross-tenant;
    # the provenance marker is what confines the delete to this migration's own rows.
    profile_model.objects.filter(provenance=BACKFILL_PROVENANCE).delete()
    engagement_model.objects.filter(provenance=BACKFILL_PROVENANCE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0012_clientcontact_engagement_taxprofile"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
