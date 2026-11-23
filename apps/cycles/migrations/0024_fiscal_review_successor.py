"""AXRM-048: extend fiscal reviews without rewriting a historical row."""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.exceptions import IrreversibleError

from apps.cycles.fiscal_review_reason import REVIEW_REASON_NONBLANK_V1

TABLE = "cycles_fiscaldocumentreview"
CHAIN_FK = "fiscaldocumentreview_supersedes_chain_fk"


def refuse_reverse_with_successors(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Fence concurrent inserts before checking that reversal preserves history."""
    schema_editor.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    review = apps.get_model("cycles", "FiscalDocumentReview")
    # CROSS_TENANT_OK: one successor in any firm prevents reversal.
    if (
        review._base_manager.using(schema_editor.connection.alias)
        .filter(supersedes_id__isnull=False)
        .exists()
    ):
        message = "Fiscal review successors exist; keep them and use forward repair."
        raise IrreversibleError(message)


class Migration(migrations.Migration):
    dependencies = [("cycles", "0023_fiscal_document_review")]

    operations = [
        migrations.AddField(
            model_name="fiscaldocumentreview",
            name="supersedes",
            field=models.ForeignKey(
                to="cycles.fiscaldocumentreview",
                on_delete=models.PROTECT,
                null=True,
                blank=True,
                related_name="+",
                db_constraint=False,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="fiscaldocumentreview",
            name="fiscaldocumentreview_revision_uniq",
        ),
        migrations.AddConstraint(
            model_name="fiscaldocumentreview",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "revision", "cycle", "supersedes"),
                nulls_distinct=False,
                name="fiscaldocumentreview_revision_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="fiscaldocumentreview",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "revision", "cycle", "id"),
                name="fiscaldocumentreview_chain_id_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="fiscaldocumentreview",
            constraint=models.CheckConstraint(
                condition=~models.Q(supersedes=models.F("id")),
                name="fiscaldocumentreview_not_self",
            ),
        ),
        migrations.AddConstraint(
            model_name="fiscaldocumentreview",
            constraint=models.CheckConstraint(
                condition=models.Q(supersedes__isnull=True)
                | models.Q(note__regex=REVIEW_REASON_NONBLANK_V1),
                name="fiscaldocumentreview_successor_reason",
            ),
        ),
        migrations.RunSQL(
            f"""ALTER TABLE {TABLE} ADD CONSTRAINT {CHAIN_FK}
            FOREIGN KEY (tenant_id, client_id, revision_id, cycle_id, supersedes_id)
            REFERENCES {TABLE} (tenant_id, client_id, revision_id, cycle_id, id);""",
            f"ALTER TABLE {TABLE} DROP CONSTRAINT {CHAIN_FK};",
        ),
        # The immutable table and this BEFORE check make every edge point to an
        # already inserted row, even within one multi-row statement. Induction
        # excludes cycles (including detached components) under every insert order.
        # The exact FK confines ancestry to one chain; the NULLS NOT DISTINCT
        # unique gives that chain one root and at most one child per row. The
        # successor reason check applies independently of how a row is inserted.
        migrations.RunSQL(
            """
            CREATE FUNCTION fiscaldocumentreview_require_existing_predecessor()
            RETURNS trigger
            LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, public AS $$
            BEGIN
                IF NEW.supersedes_id IS NOT NULL AND NEW.supersedes_id <> NEW.id
                    AND NOT EXISTS (
                        SELECT 1 FROM public.cycles_fiscaldocumentreview AS predecessor
                        WHERE predecessor.tenant_id = NEW.tenant_id
                          AND predecessor.client_id = NEW.client_id
                          AND predecessor.revision_id = NEW.revision_id
                          AND predecessor.cycle_id = NEW.cycle_id
                          AND predecessor.id = NEW.supersedes_id
                    )
                THEN
                    RAISE foreign_key_violation USING
                        CONSTRAINT = 'fiscaldocumentreview_supersedes_chain_fk',
                        MESSAGE = 'Fiscal review predecessor must already exist';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER fiscaldocumentreview_predecessor_before_insert
            BEFORE INSERT ON cycles_fiscaldocumentreview FOR EACH ROW
            EXECUTE FUNCTION fiscaldocumentreview_require_existing_predecessor();
            """,
            """
            DROP TRIGGER fiscaldocumentreview_predecessor_before_insert
                ON cycles_fiscaldocumentreview;
            DROP FUNCTION fiscaldocumentreview_require_existing_predecessor();
            """,
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse_with_successors),
    ]
