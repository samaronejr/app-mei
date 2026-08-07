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

# Exempt by CONSTRAINT, not by table. `tenants_invite` entered this test's scope the
# moment W7 gave it a nullable `client_id`, and exactly one of its unique constraints
# does not name that column.
#
# The oracle argument does not reach this one, in both directions. Reaching the
# collision requires already holding the raw token -- `token` stores sha256 of
# `secrets.token_urlsafe(32)`, so a caller who can provoke `23505` here is a caller who
# could have redeemed the invitation instead, and the "one bit" is the whole credential
# rather than a leak about it.
#
# Scoping it would also break redemption outright. `resolve_invite` looks the digest up
# with NO client and NO tenant in hand -- the invitee is anonymous until the token
# resolves -- so `(client_id, token)` would permit two invitations to share a digest and
# turn that lookup into `MultipleObjectsReturned`. Global uniqueness is what makes the
# bearer token a key at all.
#
# Named one constraint at a time so this stays an exemption rather than an amnesty: a
# later `UNIQUE (tenant_id, email)` on the same table -- "one pending invite per
# address", the obvious next constraint -- IS a cross-client oracle once portal
# invitations exist, and it must fail here.
EXEMPT_CONSTRAINTS: Final[frozenset[str]] = frozenset({"tenants_invite_token_key"})


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
            if columns != ["id"]
            and CLIENT_COLUMN not in columns
            and name not in EXEMPT_CONSTRAINTS
        ]

    assert not offenders, (
        f"unique constraints not naming {CLIENT_COLUMN}: {offenders}. A collision "
        f"on one is a one-bit existence oracle for a row in another client."
    )


def test_the_constraint_exemption_is_not_a_blanket() -> None:
    # Given the named exemptions
    # Then each still exists on a real table, so a stale name cannot sit in the literal
    # pretending to justify something. And each is one constraint, never a table: the
    # only reason `tenants_invite_token_key` is here is that its key is a bearer-token
    # digest, which the table's other constraints are not.
    assert EXEMPT_CONSTRAINTS, "an empty exemption list means the wiring above is dead"
    live = {
        name
        for table in _client_scoped_tables()
        for name, _columns in _unique_constraints(table)
    }
    stale = EXEMPT_CONSTRAINTS - live
    assert not stale, {"named but not present": sorted(stale)}
    assert not EXEMPT_CONSTRAINTS & set(EXEMPT_TABLES), (
        "a table name in the constraint list would exempt nothing and read as if it did"
    )
