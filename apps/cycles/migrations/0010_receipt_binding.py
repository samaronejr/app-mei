"""Receipt requests bind to the obligation they pay for (T17, S6.5).

A `comprovante` request names one obligation of the same client while it is still
a draft; the CHECK constraint keeps the pair consistent so a `documento` request
can never silently claim a payment, and a receipt can never float unbound.
`obligation_id` is client-paired like every other client-scoped reference:
referential checks bypass RLS, so the composite constraint is what makes a
cross-client binding unrepresentable rather than merely invisible.

Both fields are nullable-safe on populated tables: `kind` defaults to
`documento`, and existing rows satisfy the CHECK with `obligation NULL`.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.core.migrations._composite_fk import add_client_composite_fk


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("cycles", "0009_client_publications"),
        ("obligations", "0019_document_client_upload"),
        ("tenants", "0010_approval_authority"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="evidencerequest",
            name="kind",
            field=models.CharField(
                choices=[
                    ("documento", "Documento"),
                    ("comprovante", "Comprovante de pagamento"),
                ],
                default="documento",
                max_length=16,
                verbose_name="kind",
            ),
        ),
        migrations.AddField(
            model_name="evidencerequest",
            name="obligation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="evidence_requests",
                to="obligations.obligation",
                verbose_name="obligation",
            ),
        ),
        migrations.AddField(
            model_name="review",
            name="payment_relevant",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="evidencerequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "comprovante"), ("obligation__isnull", False)),
                    models.Q(("kind", "documento"), ("obligation__isnull", True)),
                    _connector="OR",
                ),
                name="evidencerequest_kind_obligation_check",
            ),
        ),
        # The plain single-column FK stays (Django manages it); the composite is
        # what actually pins the referenced obligation to the request's client.
        add_client_composite_fk(
            table="cycles_evidencerequest",
            column="obligation_id",
            parent_table="obligations_obligation",
            constraint="evidencerequest_obligation_client_fk",
        ),
    ]
