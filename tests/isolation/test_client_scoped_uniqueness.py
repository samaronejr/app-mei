"""Every unique constraint on a client-scoped table names `client_id`.

C4's remaining half. A unique constraint is an oracle: the difference between `23505`
and `INSERT 0 1` is one bit of information about a row the caller cannot read. Scoped to
`(tenant_id, storage_key)` it answers "does this key exist anywhere in the firm"; scoped
to `(tenant_id, sha256)` it answers something worse — that another client holds a
byte-identical document.

Leading with `client_id` makes the collision only ever reachable within the caller's own
client, where it discloses nothing they did not already have.

This is a meta-test rather than a per-table assertion on purpose. The constraints
themselves shipped with the document table, and asserting those specific ones would pin
what already exists while leaving the next table free to reintroduce the oracle. What is
pinned here is the *property*, over whatever set of tables carries a client dimension.
"""

from typing import Final

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db

CLIENT_COLUMN: Final = "client_id"

# The client registry itself. Its primary key IS the client identity, so a unique
# constraint on it cannot name client_id -- there is no such column, and `id` already
# carries the meaning. Same reasoning as W7's foreign-key exemption.
EXEMPT_TABLES: Final[frozenset[str]] = frozenset({"clients_clientcompany"})


def _client_scoped_tables() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_attribute a ON a.attrelid = c.oid
             WHERE n.nspname = 'public'
               AND c.relkind = 'r'
               AND a.attname = %s
               AND NOT a.attisdropped
             ORDER BY c.relname
            """,
            [CLIENT_COLUMN],
        )
        return [row[0] for row in cursor.fetchall()]


def _unique_constraints(table: str) -> list[tuple[str, list[str]]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT con.conname,
                   array_agg(att.attname ORDER BY att.attname)
              FROM pg_constraint con
              JOIN pg_class c ON c.oid = con.conrelid
              JOIN pg_attribute att
                ON att.attrelid = c.oid AND att.attnum = ANY(con.conkey)
             WHERE c.relname = %s
               AND con.contype IN ('u', 'p')
             GROUP BY con.conname
             ORDER BY con.conname
            """,
            [table],
        )
        return [(name, list(columns)) for name, columns in cursor.fetchall()]


def test_the_enumeration_finds_the_tables_it_is_meant_to_cover() -> None:
    # Given the live schema
    tables = _client_scoped_tables()

    # Then it is not empty, which is the failure mode a meta-test dies of quietly:
    # an enumeration that matches nothing passes every assertion beneath it
    assert tables, "no client-scoped tables found; the query, not the schema, is wrong"
    assert "obligations_document" in tables


def test_no_unique_constraint_omits_the_client_column() -> None:
    # Enumerated inside the test, not in a parametrize: a decorator argument is
    # evaluated at collection time, where database access is refused -- and a query
    # that raises there takes the whole module down rather than failing one case.
    offenders: list[str] = []
    for table in _client_scoped_tables():
        if table in EXEMPT_TABLES:
            continue
        offenders += [
            f"{table}.{name} ({', '.join(columns)})"
            for name, columns in _unique_constraints(table)
            # The primary key is `id` alone by design -- a UUIDv7 is globally unique
            # and discloses nothing, since a caller holding one learned it elsewhere.
            if columns != ["id"] and CLIENT_COLUMN not in columns
        ]

    assert not offenders, (
        f"unique constraints not naming {CLIENT_COLUMN}: {offenders}. A collision "
        f"on one is a one-bit existence oracle for a row in another client."
    )
