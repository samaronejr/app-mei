"""Replaying T53 restores runtime policy access and keeps the portal out."""

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)
PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def test_replayed_notice_policy_restores_exact_role_privileges() -> None:
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        call_command(
            "migrate", "delivery", "0005_outbound_retry", interactive=False, verbosity=0
        )
    finally:
        try:
            call_command("migrate", interactive=False, verbosity=0)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE app_runtime")

    actual = {}
    expected = {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("app_runtime",)
        for role in ("app_runtime", "app_portal"):
            for privilege in PRIVILEGES:
                cursor.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    [role, "delivery_tenantnoticepolicy", privilege],
                )
                actual[role, privilege] = cursor.fetchone()[0]
                expected[role, privilege] = role == "app_runtime" and privilege in {
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                }
    assert actual == expected
