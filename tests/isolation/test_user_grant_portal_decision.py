"""The central portal NONE declarations must agree and remain denied in SQL."""

import re
from pathlib import Path

import pytest
from django.db import connection

from apps.core.checks import _roles_sql_grants
from tests import conftest

TABLE = "authz_usercapabilitygrant"


def test_portal_none_registry_is_mirrored_without_allowlisting() -> None:
    sql = (Path(__file__).resolve().parents[2] / "ops/sql/roles.sql").read_text()
    # PORTAL_NONE is a machine-read declaration, not an executable GRANT.
    denied = set(re.findall(r"^-- PORTAL_NONE: ([a-z_]+)$", sql, re.MULTILINE))
    assert TABLE in denied
    assert denied == set(getattr(conftest, "PORTAL_DENIED_TABLES", ()))
    assert denied.isdisjoint(conftest.PORTAL_TABLES)
    assert denied.isdisjoint(conftest.PORTAL_WRITE_TABLES)
    assert denied.isdisjoint(table for table, _privilege in _roles_sql_grants())


@pytest.mark.django_db
def test_portal_has_no_privileges_on_user_capability_grants() -> None:
    with connection.cursor() as cursor:
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ):
            cursor.execute(
                "SELECT has_table_privilege('app_portal', %s, %s)", [TABLE, privilege]
            )
            assert cursor.fetchone() == (False,), privilege
