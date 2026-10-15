"""A new approved publication references the old bytes without rewriting receipts."""

from importlib import import_module

from django.db import migrations, models
from django.db.models.deletion import PROTECT

from apps.core.migrations._composite_fk import add_client_composite_fk

previous = import_module("apps.cycles.migrations.0012_retention_content_guards")
PREVIOUS_SQL = previous.PUBLICATION.replace(
    "BEGIN\n", "BEGIN\n" + previous.PUBLICATION_GUARD, 1
)
SQL = PREVIOUS_SQL.replace(
    "OR effect->'payload' ? 'withdrawal' THEN",
    """OR effect->'payload' ? 'withdrawal'
            OR effect->'payload'->'supersedes'->>'publication_id'
                IS DISTINCT FROM NEW.supersedes_id::text
            OR (NEW.supersedes_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM cycles_clientpublication p
                 WHERE p.tenant_id=NEW.tenant_id AND p.client_id=NEW.client_id
                   AND p.id=NEW.supersedes_id AND p.cycle_id=NEW.cycle_id
                   AND p.state='publicada'
                   AND p.publication_digest=effect->'payload'->'supersedes'->>'digest'
                   AND p.version::text=effect->'payload'->'supersedes'->>'version'
            )) THEN""",
)


class Migration(migrations.Migration):
    """Keep the prior redaction guard when applying and reversing this link."""

    dependencies = [("cycles", "0012_retention_content_guards")]
    operations = [
        migrations.AddField(
            model_name="clientpublication",
            name="supersedes",
            field=models.ForeignKey(
                to="cycles.clientpublication",
                on_delete=PROTECT,
                related_name="revisions",
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="clientpublication",
            constraint=models.UniqueConstraint(
                fields=["tenant", "client", "supersedes"],
                name="publication_single_revision",
            ),
        ),
        add_client_composite_fk(
            table="cycles_clientpublication",
            column="supersedes_id",
            parent_table="cycles_clientpublication",
            constraint="publication_supersedes_scope_fk",
        ),
        migrations.RunSQL(SQL, PREVIOUS_SQL),
    ]
