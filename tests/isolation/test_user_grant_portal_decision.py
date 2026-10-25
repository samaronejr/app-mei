"""The central portal NONE declarations must agree and remain denied in SQL."""

import re
from pathlib import Path

import pytest
from django.apps import apps
from django.db import connection

from apps.core.checks import _roles_sql_grants
from tests import conftest

NONE_TABLES = (
    "authz_usercapabilitygrant",
    "clients_clientcontact",
    "fiscal_municipalityseedrow",
    "authz_capabilityseedrow",
    "integrations_providerconnection",
    "integrations_providerbudget",
    "integrations_providerreservation",
    "integrations_clientproviderlink",
    "integrations_authorityrecord",
    "integrations_firmauthorization",
    "integrations_integrationissue",
    "integrations_providercall",
    "integrations_providercallattempt",
    "integrations_providereffectbarrier",
    "integrations_providerexecutionepoch",
    "integrations_firmprovidercall",
    "integrations_firmprovidercallattempt",
)

# Tables that predate the explicit portal-decision rule (plan A7.1) and are on no
# portal list: the portal reads them only if roles.sql grants it, and it does not.
# Frozen: a new table declares `-- PORTAL_NONE:` + PORTAL_DENIED_TABLES or joins an
# allow-list instead of being added here.
UNDECLARED_LEGACY_TABLES = frozenset(
    {
        "account_emailaddress",
        "account_emailconfirmation",
        "accounts_user",
        "accounts_user_groups",
        "accounts_user_user_permissions",
        "audit_accesslog",
        "audit_datasubjectrequest",
        "audit_event",
        "audit_platformevent",
        "auth_group",
        "auth_group_permissions",
        "auth_permission",
        "authz_capability",
        "authz_rolegrant",
        "clients_onboardingitemtemplate",
        "clients_tag",
        "core_tests_exampletenantmodel",
        "django_admin_log",
        "django_celery_beat_clockedschedule",
        "django_celery_beat_crontabschedule",
        "django_celery_beat_intervalschedule",
        "django_celery_beat_periodictask",
        "django_celery_beat_periodictasks",
        "django_celery_beat_solarschedule",
        "django_content_type",
        "django_session",
        "fiscal_municipality",
        "fiscal_municipalitycapability",
        "mfa_authenticator",
        "obligations_fiscalparameter",
        "obligations_holiday",
        "obligations_obligationduerule",
        "obligations_obligationtype",
        "obligations_schedulerheartbeat",
        "tenants_invite",
        "tenants_membership",
        "tenants_tenant",
    }
)


def test_every_model_table_has_a_deliberate_portal_decision() -> None:
    tables = {
        model._meta.db_table
        for model in apps.get_models(include_auto_created=True)
        if model._meta.managed and not model._meta.proxy
    }
    declared = (
        set(conftest.PORTAL_DENIED_TABLES)
        | set(conftest.PORTAL_TABLES)
        | set(conftest.PORTAL_WRITE_TABLES)
        | set(conftest.PORTAL_COLUMN_SELECT)
    )
    assert sorted(tables - declared - UNDECLARED_LEGACY_TABLES) == []
    # The pin only shrinks: a stale or since-declared entry must leave it.
    assert sorted(UNDECLARED_LEGACY_TABLES - tables) == []
    assert sorted(UNDECLARED_LEGACY_TABLES & declared) == []


def test_portal_none_registry_is_mirrored_without_allowlisting() -> None:
    sql = (Path(__file__).resolve().parents[2] / "ops/sql/roles.sql").read_text()
    # PORTAL_NONE is a machine-read declaration, not an executable GRANT.
    denied = set(re.findall(r"^-- PORTAL_NONE: ([a-z_]+)$", sql, re.MULTILINE))
    assert set(NONE_TABLES) <= denied
    assert denied == set(getattr(conftest, "PORTAL_DENIED_TABLES", ()))
    assert denied.isdisjoint(conftest.PORTAL_TABLES)
    assert denied.isdisjoint(conftest.PORTAL_WRITE_TABLES)
    assert denied.isdisjoint(table for table, _privilege in _roles_sql_grants())


@pytest.mark.django_db
@pytest.mark.parametrize("table", NONE_TABLES)
def test_portal_none_tables_have_no_privileges(table: str) -> None:
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
                "SELECT has_table_privilege('app_portal', %s, %s)", [table, privilege]
            )
            assert cursor.fetchone() == (False,), privilege
