"""Every tenant table carries a deliberate portal decision, and every claim bites.

This is the meta-test the whole portal isolation design rests on. A Phase-3 table added
without a portal policy is fail-open in the client dimension, and nothing else would
catch it.

Six review rounds produced one recurring defect in this project: an assertion that
passes while asserting nothing. Three of them live here specifically, so each is
countered by construction rather than by care:

* A predicate comparing the WRONG column still contains every substring a loose check
  looks for — `client_id` occurs inside the GUC literal
  `current_setting('app.client_id')`, and `id` is a substring of
  `tenant_id`. Both `qual` and `with_check` therefore anchor the column as the
  LEFT operand.
* A RESTRICTIVE policy is selected here by `app_portal` being IN its `TO` clause, so
  one that also names `app_runtime` passes every shape assertion while ANDing the
  client predicate into the firm's own connection — zero rows, silently. The role set
  is therefore asserted equal, not merely matched.
* Using `conftest.PORTAL_TABLES` as the privilege oracle is tautological, because
  `conftest` issues the test-database grants from that same tuple. The oracle is
  `ops/sql/roles.sql`, the production artifact, and conftest is asserted to match it.
* `portal_decision_exemption()` matches with `fnmatchcase`, so a single `"*"` entry
  would exempt every table while leaving every other assertion here green. The exempt
  set is therefore pinned to a literal.
"""

import re
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from django.conf import settings
from django.db import connection

from apps.core.rls import PORTAL_DECISION_EXEMPT, portal_decision_exemption
from tests.conftest import PORTAL_TABLES, PORTAL_WRITE_TABLES
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_ROLE = "app_portal"
CLIENT_COLUMN = "client_id"
RUNTIME_ROLE = "app_runtime"

EXPECTED_TENANT_TABLE_COUNT = 15

# Pinned deliberately. Widening an exemption must be a two-file edit, because
# PORTAL_DECISION_EXEMPT is glob-matched and a single "*" would otherwise void every
# branch assertion below while this module stayed green.
EXPECTED_EXEMPT_TABLES = frozenset(
    {
        "audit_accesslog",
        "audit_platformevent",
        "audit_datasubjectrequest",
        "tenants_membership",
        "tenants_invite",
        "core_tests_exampletenantmodel",
    },
)

# The client identity column differs for the registry itself: ClientCompany has no
# client_id because it *is* the client.
CLIENT_IDENTITY_COLUMN = {"clients_clientcompany": "id"}

WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES")
# The flat-zero set: everything above except the one privilege Phase 2b grants.
NON_INSERT_WRITE_PRIVILEGES = tuple(p for p in WRITE_PRIVILEGES if p != "INSERT")


def _tables_with_tenant_id() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
            "AND a.attname = 'tenant_id' AND NOT a.attisdropped) "
            "ORDER BY c.relname",
        )
        return [str(row[0]) for row in cursor.fetchall()]


def _all_public_tables() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "ORDER BY tablename",
        )
        return [str(row[0]) for row in cursor.fetchall()]


def _has_column(table: str, column: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(%s) AND a.attname = %s "
            "AND NOT a.attisdropped)",
            [f"public.{table}", column],
        )
        row = cursor.fetchone()
    return bool(row[0]) if row else False


class PortalPolicy(NamedTuple):
    name: str
    permissive: str
    cmd: str
    qual: str | None
    with_check: str | None
    roles: frozenset[str]


def _portal_policies(table: str) -> list[PortalPolicy]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT policyname, permissive, cmd, qual, with_check, roles "
            "FROM pg_policies WHERE schemaname = 'public' AND tablename = %s "
            "AND %s = ANY(roles) ORDER BY policyname",
            [table, PORTAL_ROLE],
        )
        return [
            PortalPolicy(
                str(r[0]),
                str(r[1]),
                str(r[2]),
                r[3],
                r[4],
                frozenset(str(role) for role in r[5]),
            )
            for r in cursor.fetchall()
        ]


def _rls_flags(table: str) -> tuple[bool, bool]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = to_regclass(%s)",
            [f"public.{table}"],
        )
        row = cursor.fetchone()
    assert row is not None, f"{table} is not in pg_class"
    return (bool(row[0]), bool(row[1]))


def _has_privilege(table: str, privilege: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege(%s, %s, %s)",
            [PORTAL_ROLE, f"public.{table}", privilege],
        )
        row = cursor.fetchone()
    return bool(row[0]) if row else False


EXPECTED_GRANT_LOOPS: Final[frozenset[str]] = frozenset(
    {"portal_table", "portal_write_table"},
)


def _grant_loops_from_roles_sql() -> dict[str, frozenset[str]]:
    """Read EVERY grant loop out of the production artifact, keyed on its variable.

    `finditer`, not `search`, and this is the whole point of the function. roles.sql now
    carries two loops -- the SELECT allow-list and the INSERT one -- and `re.search`
    returns only the first match. A generic pattern with `search` would pin the read
    list and leave the write list pinned by NOTHING, while every assertion here stayed
    green: the write list would be free to name any table at all.

    Keyed on the loop variable rather than on position, because "the first block" and
    "the second block" are not stable facts about a file somebody will edit.
    """
    source = Path(settings.BASE_DIR) / "ops" / "sql" / "roles.sql"
    text = source.read_text()
    loops = {
        match.group(1): frozenset(re.findall(r"'([a-z0-9_]+)'", match.group(2)))
        for match in re.finditer(
            r"FOREACH\s+([a-z_]+)\s+IN\s+ARRAY\s+ARRAY\[(.*?)\]",
            text,
            re.DOTALL,
        )
    }

    # Asserted rather than assumed. A renamed or deleted loop would otherwise make the
    # comparisons below vacuous -- an empty set equals an empty set.
    missing = EXPECTED_GRANT_LOOPS - loops.keys()
    assert not missing, (
        f"roles.sql no longer contains grant loops for {sorted(missing)}"
    )
    return loops


def _allow_list_from_roles_sql() -> frozenset[str]:
    """The SELECT allow-list, from the production artifact rather than the fixture."""
    return _grant_loops_from_roles_sql()["portal_table"]


def _write_allow_list_from_roles_sql() -> frozenset[str]:
    """The INSERT allow-list, from the production artifact rather than the fixture."""
    return _grant_loops_from_roles_sql()["portal_write_table"]


def _anchored(column: str, expression: str) -> bool:
    return re.match(rf"^\(\s*{re.escape(column)}\s*=", expression) is not None


def test_the_enumeration_is_not_empty_and_has_not_silently_shrunk() -> None:
    # Given the catalog
    assert_isolated_role()

    # When tables carrying tenant_id are enumerated
    tables = _tables_with_tenant_id()

    # Then the count is exactly what the test database should hold. `in {13, 14}` would
    # let a real tenant table disappear while the example model was present.
    assert tables
    assert len(tables) == EXPECTED_TENANT_TABLE_COUNT, sorted(tables)


def test_the_exempt_set_is_pinned_against_a_literal() -> None:
    # Given the exemption map
    assert_isolated_role()

    # When its keys are compared to the literal above
    # Then they match exactly. Without this, PORTAL_DECISION_EXEMPT["*"] = "x" exempts
    # every table and every other assertion in this module stays green.
    assert set(PORTAL_DECISION_EXEMPT) == EXPECTED_EXEMPT_TABLES


def test_every_exemption_carries_a_justification() -> None:
    # Given the exemption map
    assert_isolated_role()

    # When each justification is read
    # Then none is blank
    for table, justification in PORTAL_DECISION_EXEMPT.items():
        assert justification.strip(), table


def test_every_tenant_table_has_a_deliberate_portal_decision() -> None:
    # Given every table carrying tenant_id
    assert_isolated_role()

    for table in _tables_with_tenant_id():
        # When the exemption is consulted FIRST
        if portal_decision_exemption(table) is not None:
            continue

        policies = _portal_policies(table)
        assert policies, f"{table} has no portal policy and no exemption"

        is_client_scoped = table in CLIENT_IDENTITY_COLUMN or _has_column(
            table,
            "client_id",
        )
        column = CLIENT_IDENTITY_COLUMN.get(table, "client_id")

        for name, permissive, cmd, qual, with_check, roles in policies:
            assert permissive == "RESTRICTIVE", f"{table}.{name} is not restrictive"
            assert cmd == "ALL", f"{table}.{name} covers only {cmd}"
            assert qual is not None, f"{table}.{name} has no USING"
            assert with_check is not None, f"{table}.{name} has no WITH CHECK"
            # The TO clause names the portal and NOTHING else. A restrictive policy
            # applies to every role it lists, so one naming app_runtime as well would
            # AND this client predicate into the firm's own connection — where
            # app.client_id is unset, the comparison is NULL, and RLS admits a row only
            # on true. The firm would read zero rows with nothing raised. That is the
            # same outcome as an inheriting grant, reached by authoring a policy rather
            # than by granting a role, and the selection below matches on containment
            # so every other assertion here passes on such a policy.
            assert roles == {PORTAL_ROLE}, (
                f"{table}.{name} is restrictive and also binds {sorted(roles)}"
            )

            if is_client_scoped:
                # Then the predicate confines the portal to one client, on BOTH sides.
                for side, expression in (("USING", qual), ("WITH CHECK", with_check)):
                    assert "NULLIF(" in expression.upper(), f"{table}.{name} {side}"
                    assert ", true)" in expression, f"{table}.{name} {side}"
                    assert "app.client_id" in expression, f"{table}.{name} {side}"
                    assert _anchored(column, expression), (
                        f"{table}.{name} {side} does not compare {column} as its left "
                        f"operand: {expression}"
                    )
            else:
                # Then the portal is denied outright, by shape and not by presence.
                assert qual.strip().lower() == "false", f"{table}.{name} USING"
                assert with_check.strip().lower() == "false", f"{table}.{name} CHECK"


def test_row_level_security_is_live_on_every_policed_table() -> None:
    # Given every table carrying a portal policy
    assert_isolated_role()

    for table in _all_public_tables():
        if not _portal_policies(table):
            continue

        # When its pg_class flags are read
        enabled, forced = _rls_flags(table)

        # Then the policy is actually evaluated. A policy on a table with RLS disabled
        # is stored and never consulted: it reads as covered while protecting
        # nothing.
        assert enabled, f"{table} carries a portal policy but RLS is disabled"
        assert forced, f"{table} carries a portal policy but RLS is not forced"


def test_every_portal_policy_is_restrictive() -> None:
    # Given every policy naming the portal role anywhere in the schema
    assert_isolated_role()

    for table in _all_public_tables():
        for policy in _portal_policies(table):
            # Then none is permissive. A permissive portal policy ORs with the Phase-1
            # tenant policy and drops TENANT isolation for portal sessions, while every
            # per-table shape assertion above still passes.
            assert policy.permissive == "RESTRICTIVE", (
                f"{table}.{policy.name} is permissive"
            )

            # And none binds a second role. This sweep is the only one that reaches
            # tables the tenant_id enumeration never sees, so a dual-role policy on one
            # of those is invisible to every other assertion in this module.
            extra = sorted(policy.roles - {PORTAL_ROLE})
            assert policy.roles == {PORTAL_ROLE}, (
                f"{table}.{policy.name} also binds {extra}"
            )


def test_the_conftest_allow_list_matches_the_production_artifact() -> None:
    # Given roles.sql, which is what actually grants in dev and staging
    assert_isolated_role()

    # When its grant loop is parsed
    granted = _allow_list_from_roles_sql()

    # Then the test fixture grants exactly the same six tables. conftest issues the
    # test-database grants from PORTAL_TABLES, so using it as its own oracle could
    # never detect divergence from production.
    assert granted == set(PORTAL_TABLES), granted.symmetric_difference(PORTAL_TABLES)


# W1b. An INDEPENDENT literal, and that is the whole requirement. Defined as
# `_portal_policied_tables() - WRITE_ALLOWLIST` the assertion below would compare a
# thing to itself and could never fail -- the defect this project keeps finding, in the
# requirement written to prevent it. Spelled out, a table dropping out of runtime
# coverage, or quietly gaining a write grant, turns the meta-test red.
STATIC_ONLY_TABLES: Final[frozenset[str]] = frozenset(
    {
        "audit_event",
        "clients_clientassignment",
        "clients_clientcompany",
        "clients_clienttag",
        "clients_onboardingitem",
        "clients_tag",
        "obligations_monthlyrevenue",
        "obligations_obligation",
    },
)

# W7. Foreign keys on a portal-writable table that CANNOT be client-paired, with the
# reason for each. Shaped like PORTAL_DECISION_EXEMPT: a literal carrying a
# justification, so the decision is made once and visibly rather than ad hoc.
CLIENT_PAIRING_EXEMPT: Final[dict[str, str]] = {
    "tenant_id": (
        "tenants_tenant has no client_id; the tenant IS the wider scope, and pairing "
        "the client into a reference to it is not expressible"
    ),
    "client_id": (
        "clients_clientcompany is the client registry itself: its primary key IS the "
        "client identity, so the correct form is the tenant-paired one it carries"
    ),
    "uploaded_by_id": (
        "accounts_user is platform-level and has no client_id. A user is not owned by "
        "a client; the row's own client_id is what confines the document"
    ),
}


def _portal_policed_tables() -> frozenset[str]:
    """Every table carrying a policy bound to `app_portal`, from the catalog."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT c.relname FROM pg_policy p "
            "JOIN pg_class c ON c.oid = p.polrelid "
            "JOIN pg_roles r ON r.oid = ANY(p.polroles) "
            "WHERE r.rolname = %s",
            [PORTAL_ROLE],
        )
        return frozenset(str(row[0]) for row in cursor.fetchall())


def _foreign_keys(table: str) -> list[tuple[str, list[str]]]:
    """Return (constraint name, key columns) for every FK on the table."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT con.conname, array_agg(att.attname ORDER BY att.attnum)
              FROM pg_constraint con
              JOIN pg_class c ON c.oid = con.conrelid
              JOIN pg_attribute att
                ON att.attrelid = c.oid AND att.attnum = ANY(con.conkey)
             WHERE c.relname = %s AND con.contype = 'f'
             GROUP BY con.conname
            """,
            [table],
        )
        return [(str(name), list(cols)) for name, cols in cursor.fetchall()]


def test_w1b_the_static_only_list_is_the_policed_set_minus_the_write_list() -> None:
    # Given the policed set read from the catalog and the write list from roles.sql
    assert_isolated_role()
    policed = _portal_policed_tables()
    writable = _write_allow_list_from_roles_sql()

    # Then the independent literal equals the difference. Deriving the literal instead
    # would make this `x == x`.
    assert policed - writable == STATIC_ONLY_TABLES, {
        "only_in_literal": sorted(STATIC_ONLY_TABLES - (policed - writable)),
        "only_in_catalog": sorted((policed - writable) - STATIC_ONLY_TABLES),
    }


def test_w1b_no_static_only_table_is_writable_by_the_portal() -> None:
    # Given the tables whose WITH CHECK is asserted statically and never exercised
    assert_isolated_role()

    for table in sorted(STATIC_ONLY_TABLES):
        for privilege in WRITE_PRIVILEGES:
            # Then the portal cannot write to any of them. Their policies are correct
            # and unexercisable, which is only safe while this holds.
            assert not _has_privilege(table, privilege), f"{table} {privilege}"


def test_w7_every_foreign_key_on_a_writable_table_is_paired_or_exempt() -> None:
    # Given every table the portal may write into
    assert_isolated_role()

    offenders: list[str] = []
    for table in sorted(_write_allow_list_from_roles_sql()):
        paired: set[str] = set()
        for _name, columns in _foreign_keys(table):
            if CLIENT_COLUMN in columns:
                paired.update(columns)
        for _name, columns in _foreign_keys(table):
            if CLIENT_COLUMN in columns:
                continue
            offenders += [
                f"{table}.{column}"
                for column in columns
                if column not in paired and column not in CLIENT_PAIRING_EXEMPT
            ]

    # Then each is either covered by a constraint whose key includes client_id, or named
    # in the exemption literal with its reason. Without the literal this gets relaxed ad
    # hoc the first time somebody meets tenant_id -> tenants_tenant.
    assert not offenders, (
        f"FK columns neither client-paired nor exempt: {sorted(set(offenders))}"
    )


def test_w7_the_exemption_list_is_not_a_blanket() -> None:
    # Given the exemptions
    assert_isolated_role()

    # Then the one column that CAN be paired is not among them. An exemption list that
    # happened to name every column would satisfy the test above while exempting the
    # control entirely.
    assert "obligation_id" not in CLIENT_PAIRING_EXEMPT
    assert all(reason.strip() for reason in CLIENT_PAIRING_EXEMPT.values())


def test_the_conftest_write_allow_list_matches_the_production_artifact() -> None:
    # Given roles.sql's INSERT loop, which is what actually grants in dev and staging
    assert_isolated_role()

    # Then the fixture grants exactly the same tables. conftest issues the test-database
    # INSERT grants from PORTAL_WRITE_TABLES, so using it as its own oracle could never
    # detect divergence -- the same trap the read list is already pinned against.
    granted = _write_allow_list_from_roles_sql()
    assert granted == set(PORTAL_WRITE_TABLES), granted.symmetric_difference(
        PORTAL_WRITE_TABLES,
    )


def test_the_write_allow_list_is_a_subset_of_the_read_one() -> None:
    # Given both loops
    assert_isolated_role()

    # Then nothing is writable that is not also readable. A table the portal may INSERT
    # into but not SELECT from would make its own WITH CHECK refusals unverifiable from
    # the portal's side, and would be a shape nobody chose deliberately.
    assert _write_allow_list_from_roles_sql() <= _allow_list_from_roles_sql()


def test_the_portal_role_can_read_the_allow_list_and_nothing_else() -> None:
    # Given the allow-list taken from roles.sql
    assert_isolated_role()
    allow_listed = _allow_list_from_roles_sql()

    for table in _all_public_tables():
        # When SELECT is checked in both directions
        readable = _has_privilege(table, "SELECT")

        # Then exactly the allow-listed tables are readable. This is the only assertion
        # that sees accounts_user, mfa_authenticator and django_session, none of which
        # carries tenant_id and none of which the enumeration above reaches.
        assert readable == (table in allow_listed), table


def test_the_portal_role_holds_insert_only_on_the_write_allow_list() -> None:
    """INSERT is granted exactly where the allow-list says and nowhere else.

    Split from the flat-zero sweep below when Phase 2b granted the first write. The two
    assertions are STRUCTURALLY separate on purpose: folded together, widening the
    allow-list would silently relax the flat-zero guarantee for every other privilege,
    and the test would still pass.
    """
    assert_isolated_role()

    granted = {
        table for table in _all_public_tables() if _has_privilege(table, "INSERT")
    }
    # Compared against roles.sql rather than the fixture: conftest issues these grants,
    # so comparing them to conftest would be comparing a thing to itself.
    assert granted == set(_write_allow_list_from_roles_sql()), sorted(granted)


def test_the_portal_role_holds_no_other_write_privilege_anywhere() -> None:
    """DELETE, TRUNCATE, REFERENCES and UPDATE are flat zero at TABLE level.

    UPDATE is in this set deliberately: `UPDATE documents SET status='deleted'` is a
    delete in every sense this product cares about. If metadata editing is ever needed
    it must be a COLUMN-level grant, which `has_table_privilege` reports as false and
    `has_any_column_privilege` reports separately -- verified on the cluster, so
    flat-zero and a column grant are compatible rather than contradictory.

    REFERENCES is in it because a role holding it can create a foreign key to a table it
    cannot read and use the violations as an existence oracle.
    """
    assert_isolated_role()

    for table in _all_public_tables():
        for privilege in NON_INSERT_WRITE_PRIVILEGES:
            assert not _has_privilege(table, privilege), f"{table} {privilege}"


def test_the_portal_role_holds_no_sequence_privilege() -> None:
    """A granted sequence's `last_value` is a global cross-client row counter.

    Readable by a role that can see zero rows, which is why every primary key here is a
    UUIDv7 and no sequence privilege is granted anywhere.
    """
    assert_isolated_role()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'S'",
        )
        sequences = [str(row[0]) for row in cursor.fetchall()]
        for sequence in sequences:
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                cursor.execute(
                    "SELECT has_sequence_privilege(%s, %s, %s)",
                    [PORTAL_ROLE, sequence, privilege],
                )
                assert cursor.fetchone()[0] is False, f"{sequence} {privilege}"


def test_the_portal_grant_does_not_inherit_into_the_runtime_role() -> None:
    # Given the role grant
    assert_isolated_role()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_has_role(%s, %s, 'USAGE'), pg_has_role(%s, %s, 'SET')",
            [RUNTIME_ROLE, PORTAL_ROLE, RUNTIME_ROLE, PORTAL_ROLE],
        )
        row = cursor.fetchone()
    assert row is not None
    inherits, can_set = bool(row[0]), bool(row[1])

    # Then app_runtime can ENTER the portal role but does not INHERIT its privileges.
    # PostgreSQL matches a policy's TO clause by inheritance, not identity: with a
    # default GRANT, every RESTRICTIVE portal policy would bind app_runtime too and the
    # accounting firm would read zero rows from every client table.
    assert not inherits, "app_runtime inherits app_portal: the firm will see no rows"
    assert can_set, "app_runtime cannot SET ROLE app_portal: the portal cannot work"
