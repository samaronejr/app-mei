"""Replaying T18 restores runtime access without exposing evidence to the portal."""

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def test_replayed_evidence_tables_restore_exact_role_privileges() -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        call_command("migrate", "cycles", "0010", interactive=False, verbosity=0)
    finally:
        try:
            call_command("migrate", interactive=False, verbosity=0)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")

    immutable = {"cycles_evidenceindex", "cycles_completiongeneration"}
    tables = immutable | {
        "cycles_evidenceexport",
        "cycles_exportattempt",
        "cycles_completionjustification",
        "cycles_legalhold",
        "cycles_contentpurgeclaim",
    }
    privileges = (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    )
    actual = {}
    expected = {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("app_runtime",)
        for table in sorted(tables):
            for role in ("app_runtime", "app_portal"):
                for privilege in privileges:
                    cursor.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        [role, table, privilege],
                    )
                    actual[table, role, privilege] = cursor.fetchone()[0]
                    expected[table, role, privilege] = role == "app_runtime" and (
                        privilege in {"SELECT", "INSERT"}
                        or (
                            table not in immutable and privilege in {"UPDATE", "DELETE"}
                        )
                    )
    assert actual == expected
