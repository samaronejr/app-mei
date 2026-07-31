"""Issue the portal grants `ops/sql/roles.sql` was never in a position to issue.

roles.sql runs from the Postgres init hook against an EMPTY data directory under
`ON_ERROR_STOP=1`, so its two `GRANT` blocks are `to_regclass`-guarded and therefore
reach nothing at all on a fresh cluster. `migrate` creates the tables afterwards, and
each one arrives with the RESTRICTIVE policies its migration declares and no grant.
`core.E012` sees that and refuses to boot -- correctly, but it is a backstop, not a
fix, and on the documented `docker compose down -v && up -d --wait` path it turns the
bootstrap itself into a refusal.

`apply_portal_grants` closes the loop where the gap opens: `post_migrate`, immediately
after the migration that created the table. Two properties are structural rather than
incidental.

**It must never abort a migrate.** `post_migrate` runs inside the `migrate` command, so
an exception here is a deployment that cannot migrate. Every failure mode -- another
vendor, a cluster with no `app_portal`, a table that does not exist yet, an unreachable
database -- resolves to "do nothing", mirroring `core.E012`'s own fail-open posture.

**It must be near-free.** `post_migrate` is emitted by `flush` in
`TransactionTestCase._fixture_teardown` as well as by `migrate`, so this receiver runs
roughly 470 times per test suite. The roles.sql parse is memoised here rather than in
`apps.core.checks`, whose own caller reads it once per `manage.py check`; the missing
pairs are computed in ONE catalog query; and a clean database costs two round-trips and
issues no DDL.
"""

import logging
import re
from functools import lru_cache
from typing import Any

from django.apps.config import AppConfig
from django.db import DatabaseError, connections
from django.db.backends.utils import CursorWrapper

from apps.core.checks import _roles_sql_grants

logger = logging.getLogger(__name__)

PORTAL_ROLE = "app_portal"

# Identifiers cannot be bound as query parameters, so table names and privileges are
# interpolated into the DDL below. This pair of filters is that interpolation's only
# boundary: roles.sql is a trusted artifact today, and a regex drift there must not
# become a SQL-injection seam here.
_IDENTIFIER = re.compile(r"^[a-z0-9_]+$")
_PRIVILEGES = frozenset({"SELECT", "INSERT"})


@lru_cache(maxsize=1)
def _declared_grants() -> tuple[tuple[str, str], ...]:
    """Return the well-formed (table, privilege) pairs roles.sql declares."""
    return tuple(
        sorted(
            (table, privilege)
            for table, privilege in _roles_sql_grants()
            if _IDENTIFIER.fullmatch(table) and privilege in _PRIVILEGES
        ),
    )


def _missing_pairs(
    cursor: CursorWrapper,
    declared: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """Return the declared pairs the portal role does not hold, in ONE query.

    `has_table_privilege` is called through its **regclass/OID** overload rather than
    its text one. PostgreSQL does not short-circuit `WHERE`, so the text form raises
    `relation ... does not exist` on a table no migration has created yet even when
    guarded by a `to_regclass(...) IS NOT NULL` conjunct in the same predicate. The OID
    overload answers NULL for a NULL regclass, so a missing table fails the `IS FALSE`
    test and filters itself out.
    """
    values = ", ".join(["(%s::text, %s::text)"] * len(declared))
    cursor.execute(
        # S608 is suppressed because the interpolated fragment is a fixed placeholder
        # pair repeated len(declared) times and carries no data at all. Every table and
        # privilege travels as a bound parameter, cast to ::text because an
        # unknown-typed literal in a VALUES list is resolved per column, not per row.
        "SELECT t.tbl, t.priv "  # noqa: S608
        f"FROM (VALUES {values}) AS t(tbl, priv) "
        "WHERE has_table_privilege(%s, to_regclass('public.' || t.tbl), t.priv) "
        "IS FALSE",
        [item for pair in declared for item in pair] + [PORTAL_ROLE],
    )
    return [(str(table), str(privilege)) for table, privilege in cursor.fetchall()]


def apply_portal_grants(
    sender: AppConfig | None,  # noqa: ARG001
    using: str,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Grant the portal role every allow-listed privilege it is missing.

    Connected to `post_migrate` for the core `AppConfig` alone, so it fires once per
    `migrate` invocation rather than once per installed app -- and, unavoidably, once
    per `TransactionTestCase` flush teardown, which is why the clean path issues no DDL
    and reads nothing from disk.
    """
    connection = connections[using]
    if connection.vendor != "postgresql":
        return

    declared = _declared_grants()
    if not declared:
        return

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                [PORTAL_ROLE],
            )
            if cursor.fetchone() is None:
                logger.warning(
                    "Portal grants not applied: the %s role does not exist. "
                    "Run ops/sql/roles.sql as the bootstrap superuser.",
                    PORTAL_ROLE,
                )
                return

            missing = _missing_pairs(cursor, declared)
            for table, privilege in missing:
                cursor.execute(
                    f'GRANT {privilege} ON public."{table}" TO "{PORTAL_ROLE}"',
                )
            if missing:
                logger.info(
                    "Applied %s portal grant(s) roles.sql could not reach: %s",
                    len(missing),
                    ", ".join(f"{table} {privilege}" for table, privilege in missing),
                )
    except DatabaseError:
        logger.warning(
            "Portal grants not applied: the database refused the grant probe. "
            "core.E012 still refuses to boot if the grants really did drift.",
            exc_info=True,
        )
