"""Keep provenance, holds and recovery descriptors beside retained originals."""

from django.db import migrations

TABLES = (
    "cycles_documentintake",
    "cycles_extractedfield",
    "cycles_review",
    "cycles_internalnote",
    "cycles_requestinternal",
    "cycles_exceptioninternal",
    "cycles_legalhold",
    "cycles_evidenceexport",
    "cycles_exportattempt",
)


class Migration(migrations.Migration):
    """Redaction changes content; ordinary DELETE cannot discard its identity."""

    dependencies = [("cycles", "0014_retention_claim_scope")]
    operations = [
        migrations.RunSQL(
            f"CREATE TRIGGER retained_{table} BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION cycles_retained_metadata();",
            f"DROP TRIGGER retained_{table} ON {table};",
        )
        for table in TABLES
    ]
