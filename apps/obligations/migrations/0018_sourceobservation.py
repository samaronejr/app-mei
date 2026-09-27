"""The observation table, and the legacy `paid` statuses restated as observations.

S6.4/S6.5: payment is an observed fact with a source, not a meaning folded into the
obligation's work status. Every `status='paid'` row becomes one
`SourceObservation(kind=payment, source_kind='legacy_status',
verification_status='unknown')` — labelled rather than deleted, because the flag
said "the firm recorded a payment", which is true, and nothing more, which is why
the portal renders it "Pagamento registrado manualmente (legado)" instead of
confirmed.

# CROSS_TENANT_OK: app_migrator holds BYPASSRLS and this migration's whole purpose
# is deriving rows for EVERY firm's obligations. The scope is held explicit by
# iterating tenants and filtering every query on tenant_id — no statement runs
# across tenants without the tenant column named in it.

Two rules protect the run itself, as in clients' backfill_v1. It is **idempotent**:
an obligation already carrying a `legacy_status` payment observation is skipped, so
a re-run creates nothing. And it is **asserted**: afterwards every paid obligation
must have at least one such observation — a silent partial backfill (the failure
mode BYPASSRLS exists to prevent) raises instead of reporting success over missing
rows.

`observed_at` copies the obligation's own `updated_at`: the instant the legacy row
was last written is the only timestamp the old schema ever kept for the status, and
it is used rather than now() precisely because the recorded clock
(`recorded_at`, filled by the table itself) is what says "the migration ran today".
Backdating `observed_at` to the migration instant would claim the payment was
observed today; inventing one from `competence_month` would claim a day nobody
recorded.

The reverse of the data step is a no-op: the table is append-only (the trigger
below is what makes that true for every role), and reversing the *migration* drops
the table whole, so the rows need no deleting.
"""

import django.db.models.deletion
import django.db.models.manager
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import apps.core.identifiers
from apps.audit.appendonly import make_append_only
from apps.core.migrations._composite_fk import (
    add_client_composite_fk,
    add_tenant_composite_fk,
)
from apps.core.migrations._operations import EnablePortalClientRLS, EnableRLS

OBSERVATION_TABLE = "obligations_sourceobservation"


def backfill_paid_observations(
    apps: Apps,
    _editor: BaseDatabaseSchemaEditor | None,
) -> None:
    """Assert one legacy-sourced payment observation per `paid` obligation."""
    tenant_model = apps.get_model("tenants", "Tenant")
    obligation_model = apps.get_model("obligations", "Obligation")
    observation_model = apps.get_model("obligations", "SourceObservation")

    for tenant in tenant_model.objects.order_by("slug"):
        paid = list(obligation_model.objects.filter(tenant_id=tenant.id, status="paid"))
        if not paid:
            continue

        already_observed = set(
            observation_model.objects.filter(
                tenant_id=tenant.id,
                subject_kind="obligation",
                kind="payment",
                source_kind="legacy_status",
                subject_ref_id__in=[row.id for row in paid],
            ).values_list("subject_ref_id", flat=True)
        )
        rows = [
            observation_model(
                tenant_id=tenant.id,
                client_id=obligation.client_id,
                subject_kind="obligation",
                subject_ref_id=obligation.id,
                kind="payment",
                period=obligation.competence_month,
                source_kind="legacy_status",
                observed_at=obligation.updated_at,
                value_json=(
                    {"status": "paid", "evidence_ref": obligation.evidence_ref}
                    if obligation.evidence_ref
                    else {"status": "paid"}
                ),
                verification_status="unknown",
            )
            for obligation in paid
            if obligation.id not in already_observed
        ]
        observation_model.objects.bulk_create(rows)

        # Asserted on coverage, not on creation: a re-run legitimately inserts
        # nothing, and "created == paid" would make an idempotent re-run look like
        # a failure.
        missing = [
            str(obligation.id)
            for obligation in paid
            if not observation_model.objects.filter(
                tenant_id=tenant.id,
                subject_kind="obligation",
                kind="payment",
                source_kind="legacy_status",
                subject_ref_id=obligation.id,
            ).exists()
        ]
        if missing:
            msg = (
                f"paid-obligation backfill incomplete for tenant {tenant.id}: "
                f"{len(missing)} obligations gained no observation ({missing}). "
                "Refusing to report a partial backfill."
            )
            raise AssertionError(msg)


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0014_clientcompany_tax_profile_revision"),
        ("obligations", "0017_schedulerheartbeat_free_disk_kib"),
        ("tenants", "0007_tenant_features"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SourceObservation",
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
                (
                    "subject_kind",
                    models.CharField(
                        choices=[("obligation", "obligation")],
                        default="obligation",
                        max_length=32,
                        verbose_name="subject kind",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("payment", "payment"),
                            ("filing", "filing"),
                            ("guide_issued", "guide issued"),
                        ],
                        max_length=32,
                        verbose_name="kind",
                    ),
                ),
                (
                    "period",
                    models.DateField(blank=True, null=True, verbose_name="period"),
                ),
                (
                    "source_kind",
                    models.CharField(
                        choices=[
                            ("official_api", "official API"),
                            ("official_file_import", "official file import"),
                            ("client_submitted", "client submitted"),
                            ("staff_recorded", "staff recorded"),
                            ("deterministic_rule", "deterministic rule"),
                            ("ai_inference", "AI inference"),
                            ("legacy_status", "legacy status column"),
                        ],
                        max_length=32,
                        verbose_name="source kind",
                    ),
                ),
                ("observed_at", models.DateTimeField(verbose_name="observed at")),
                (
                    "value_json",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="the asserted payload; identifiers and codes only, never text",
                        verbose_name="value",
                    ),
                ),
                (
                    "verification_status",
                    models.CharField(
                        choices=[
                            ("verified", "verified"),
                            ("pending_review", "pending review"),
                            ("unknown", "unknown"),
                            ("unsupported", "unsupported"),
                            ("stale", "stale"),
                            ("refresh_failed", "refresh failed"),
                            ("unauthorized", "unauthorized"),
                            ("not_applicable", "not applicable"),
                        ],
                        default="unknown",
                        max_length=32,
                        verbose_name="verification status",
                    ),
                ),
                (
                    "recorded_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="recorded at"),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_observations",
                        to="clients.clientcompany",
                        verbose_name="client",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="recorded_observations",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="recorded by",
                    ),
                ),
                (
                    "subject_ref",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="observations",
                        to="obligations.obligation",
                        verbose_name="subject",
                    ),
                ),
                (
                    "supersedes",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="successors",
                        to="obligations.sourceobservation",
                        verbose_name="supersedes",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="tenants.tenant",
                        verbose_name="tenant",
                    ),
                ),
            ],
            options={
                "verbose_name": "source observation",
                "verbose_name_plural": "source observations",
                "ordering": ["observed_at", "pk"],
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=["tenant", "client", "subject_ref"],
                        name="obs_tenant_client_subject_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="sourceobservation_tenant_client_id_uniq",
                    )
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("SourceObservation"),
        # S8.2 lists obligations_sourceobservation on the portal's SELECT
        # allow-list: the payments page labels "paid" from it. The RESTRICTIVE
        # client_id policy is what confines that read to the portal's own client.
        EnablePortalClientRLS("SourceObservation"),
        # Referential integrity bypasses row-level security, so each client-scoped
        # reference is composite: (tenant_id, client_id, …) must exist together or
        # the link is unrepresentable, not merely invisible.
        add_tenant_composite_fk(
            table=OBSERVATION_TABLE,
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="sourceobservation_tenant_client_fk",
        ),
        # subject_ref is pinned to obligations of the SAME client: the portal's
        # WITH CHECK inspects the row's own client_id and the reference alone would
        # otherwise be a cross-client existence oracle.
        add_client_composite_fk(
            table=OBSERVATION_TABLE,
            column="subject_ref_id",
            parent_table="obligations_obligation",
            constraint="sourceobservation_subject_tenant_client_fk",
        ),
        # The supersede chain can never leave its client either.
        add_client_composite_fk(
            table=OBSERVATION_TABLE,
            column="supersedes_id",
            parent_table=OBSERVATION_TABLE,
            constraint="sourceobservation_supersedes_tenant_client_fk",
        ),
        # Trigger + grant split: UPDATE and DELETE are refused to every role, and
        # app_runtime keeps only SELECT and INSERT. Invariant 10 lives here —
        # "manual stays manual" is enforced by supersede-never-update being the
        # only write path that exists.
        *make_append_only(OBSERVATION_TABLE),
        migrations.RunPython(backfill_paid_observations, migrations.RunPython.noop),
    ]
