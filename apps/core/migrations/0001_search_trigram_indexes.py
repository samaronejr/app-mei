"""Search support: assert the extensions, add the fold function and trigram indexes.

ADR 0005: search runs in PostgreSQL with `unaccent` and `pg_trgm`. Both need the
database superuser to install, which `app_migrator` deliberately is not, so this
migration ASSERTS them and fails with the runbook step rather than creating them
(`ops/sql/roles.sql` and `02-template1-extensions.sh` provision them; the live pilot
database needs the one-time operator step in `ops/README.md`).

`public.search_fold(text)` wraps `unaccent` with its dictionary named explicitly so
the function can be IMMUTABLE -- `unaccent(text)` itself is only STABLE, because it
resolves the dictionary through `search_path` -- which is what lets a GIN index be
built over the folded value. The indexes are trigram GIN over exactly the
expressions `apps.core.search` matches on, so no separate search index exists:
every row still comes back through the table and its row-level security.

Reversal drops the indexes, then the function -- which nothing but these indexes
and `apps.core.search` uses -- and leaves the extensions in place, since they were
never this migration's to create.
"""

from django.db import migrations

ASSERT_SEARCH_EXTENSIONS = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_extension WHERE extname = 'unaccent')
       OR NOT EXISTS (SELECT FROM pg_extension WHERE extname = 'pg_trgm') THEN
        RAISE EXCEPTION
            'search needs the unaccent and pg_trgm extensions, and app_migrator '
            'cannot create extensions (no database CREATE). The bootstrap superuser '
            'must run the CREATE EXTENSION statements in ops/sql/roles.sql against '
            'this database -- the ops/README.md runbook step -- before this '
            'migration can run.';
    END IF;
END
$$;
"""

CREATE_FOLD = """
CREATE FUNCTION public.search_fold(text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, $1)) $$;
"""

DROP_FOLD = "DROP FUNCTION public.search_fold(text);"

CLIENTS = "clients_clientcompany"
INDEXES = (
    ("search_client_legal_trgm", CLIENTS, "public.search_fold(legal_name)"),
    ("search_client_trade_trgm", CLIENTS, "public.search_fold(trade_name)"),
    ("search_client_cnpj_trgm", CLIENTS, "cnpj"),
    (
        "search_request_title_trgm",
        "cycles_evidencerequest",
        "public.search_fold(title)",
    ),
    (
        "search_document_name_trgm",
        "obligations_document",
        "public.search_fold(original_filename)",
    ),
)


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("cycles", "0008_exact_effect_approvals"),
        ("obligations", "0019_document_client_upload"),
    ]

    operations = [
        migrations.RunSQL(
            sql=ASSERT_SEARCH_EXTENSIONS, reverse_sql=migrations.RunSQL.noop
        ),
        migrations.RunSQL(sql=CREATE_FOLD, reverse_sql=DROP_FOLD),
        *(
            migrations.RunSQL(
                sql=(
                    f"CREATE INDEX {name} ON {table} "
                    f"USING gin (({expression}) public.gin_trgm_ops);"
                ),
                reverse_sql=f"DROP INDEX {name};",
            )
            for name, table, expression in INDEXES
        ),
    ]
