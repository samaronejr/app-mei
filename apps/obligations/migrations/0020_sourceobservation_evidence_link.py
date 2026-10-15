"""Observations may cite the evidence link they were asserted from (T17, S6.5).

A receipt chain -- submission, acceptance, rejection -- is only auditable when
the observation names the `EvidenceLink` it derives from. The column is
nullable: staff-recorded facts may cite no file at all.

Three guards, each for a different failure:

* the submission-only UNIQUE on (tenant, client, evidence_link) makes a submission's
  observation replay-safe even when two materializers race a retry window --
  the second sees the row under the subject lock, and the constraint is the
  floor if it ever didn't;
* the composite client FK makes a cross-client citation unrepresentable
  (referential checks bypass RLS);
* the new `calendar_scheduled` kind records that the deterministic generator
  asserted a schedule -- a fact about the calendar, never about payment.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.core.migrations._composite_fk import add_client_composite_fk


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("cycles", "0010_receipt_binding"),
        ("obligations", "0019_document_client_upload"),
        ("tenants", "0010_approval_authority"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sourceobservation",
            name="evidence_link",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="observations",
                to="cycles.evidencelink",
                verbose_name="evidence link",
            ),
        ),
        migrations.AlterField(
            model_name="sourceobservation",
            name="kind",
            field=models.CharField(
                choices=[
                    ("payment", "payment"),
                    ("filing", "filing"),
                    ("guide_issued", "guide issued"),
                    ("calendar_scheduled", "calendar scheduled"),
                ],
                max_length=32,
                verbose_name="kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="sourceobservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("evidence_link__isnull", False),
                    ("kind", "payment"),
                    ("source_kind", "client_submitted"),
                ),
                fields=("tenant", "client", "evidence_link"),
                name="sourceobservation_evidence_link_uniq",
            ),
        ),
        migrations.AddField(
            model_name="sourceobservation",
            name="outcome",
            field=models.CharField(
                choices=[
                    ("none", "no outcome recorded"),
                    ("submitted", "submitted"),
                    ("accepted", "accepted"),
                    ("rejected", "rejected"),
                    ("recorded", "recorded"),
                    ("corrected", "corrected"),
                    ("retracted", "retracted"),
                    ("scheduled", "scheduled"),
                    ("refresh_failed", "refresh failed"),
                ],
                default="none",
                max_length=32,
                verbose_name="outcome",
            ),
        ),
        migrations.AddField(
            model_name="sourceobservation",
            name="source_ref",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="source reference"
            ),
        ),
        add_client_composite_fk(
            table="obligations_sourceobservation",
            column="evidence_link_id",
            parent_table="cycles_evidencelink",
            constraint="sourceobservation_evlink_client_fk",
        ),
    ]
