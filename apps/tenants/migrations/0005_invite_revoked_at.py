"""Give an invitation a third way to end: withdrawn by the firm.

Nullable with no default and no backfill, and that is the correct reading of the
existing rows rather than a shortcut. Every invitation already in the table was issued
before revocation existed, so none of them was withdrawn; NULL says exactly that. A
default of `now()` would have claimed the opposite for all of them at once.

Additive only. Nothing is dropped and no existing column is rewritten — in particular
`expires_at` keeps its meaning, which is the whole reason a separate column is being
added instead of that one being repurposed. See the field's comment in models.py.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0004_alter_invite_id_alter_membership_id_alter_tenant_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="invite",
            name="revoked_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="revoked at",
            ),
        ),
    ]
