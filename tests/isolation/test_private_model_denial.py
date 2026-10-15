"""Real-role write denials for T38's previously unswept private models."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from django.apps import apps as django_apps
from django.db import ProgrammingError, connection, models, transaction
from psycopg import sql

from apps.core.tenancy import client_context, tenant_context
from tests.isolation.axorim_seed import SeedContext, make_seed_context, seed_model
from tests.isolation.rolecheck import assert_isolated_role

if TYPE_CHECKING:
    from django.db.backends.utils import CursorWrapper

pytestmark = pytest.mark.django_db(transaction=True)

PRIVATE_LABELS = (
    "authz.UserCapabilityGrant",
    "tenants.BillingAccount",
    "tenants.UsageRecord",
    "tenants.NotificationPreference",
    "tenants.FirmOutboundMessage",
    "tenants.AccountExport",
    "tenants.AccountExportAttempt",
)
PORTAL_NONE_LABELS = (*PRIVATE_LABELS, "clients.ClientContact")
MUTABLE_LABELS = tuple(
    label for label in PRIVATE_LABELS if label != "tenants.UsageRecord"
)


@pytest.fixture
def firms() -> dict[str, SeedContext]:
    """Seed two firms with real runtime-owned rows, independently per test."""
    return {
        "alpha": make_seed_context("private-alpha", "a"),
        "beta": make_seed_context("private-beta", "b"),
    }


def _manager(model: type[models.Model]) -> models.Manager[models.Model]:
    """Bypass the ORM tenant filter so only database controls deny access."""
    return cast("models.Manager[models.Model]", cast("object", model._base_manager))


def _copy_values(row: models.Model) -> dict[str, object]:
    """Copy valid columns; a fresh primary key leaves the target tenant intact."""
    return {
        field.attname: getattr(row, field.attname)
        for field in row._meta.concrete_fields
        if not field.primary_key
    }


def _assert_owned(row: models.Model, firm: SeedContext) -> None:
    """Read the exact seeded row back as app_runtime under its owning GUCs."""
    assert_isolated_role()
    with tenant_context(firm.tenant.pk):
        assert _manager(type(row)).filter(pk=row.pk).count() == 1


@pytest.mark.parametrize("label", PRIVATE_LABELS)
def test_foreign_insert_is_refused_by_tenant_with_check(
    label: str, firms: dict[str, SeedContext]
) -> None:
    # Given valid beta values, positively read as the unprivileged runtime role.
    model = django_apps.get_model(label)
    row = seed_model(model, firms["beta"])
    _assert_owned(row, firms["beta"])

    # When alpha inserts a new primary key with beta's tenant and valid parents.
    with (
        tenant_context(firms["alpha"].tenant.pk),
        pytest.raises(ProgrammingError) as error,
        transaction.atomic(),
    ):
        _manager(model).create(**_copy_values(row))

    # Then RLS, not a unique/FK error, refused the write, and beta's row remains.
    assert getattr(error.value.__cause__, "sqlstate", None) == "42501"
    assert "row-level security" in str(error.value)
    _assert_owned(row, firms["beta"])


@pytest.mark.parametrize("label", MUTABLE_LABELS)
def test_foreign_update_cannot_touch_an_owned_row(
    label: str, firms: dict[str, SeedContext]
) -> None:
    # Given the same legal no-op UPDATE succeeds against an owned runtime row.
    model = django_apps.get_model(label)
    row = seed_model(model, firms["beta"])
    _assert_owned(row, firms["beta"])
    with tenant_context(firms["beta"].tenant.pk):
        assert _manager(model).filter(pk=row.pk).update(id=models.F("id")) == 1

    # When alpha attempts the identical UPDATE, with no ORM scope filter.
    with tenant_context(firms["alpha"].tenant.pk):
        changed = _manager(model).filter(pk=row.pk).update(id=models.F("id"))

    # Then RLS hides the target from the write and the owner can still read it.
    assert changed == 0
    _assert_owned(row, firms["beta"])


@pytest.mark.parametrize("label", PRIVATE_LABELS)
def test_foreign_delete_cannot_remove_an_owned_row(
    label: str, firms: dict[str, SeedContext]
) -> None:
    # Given a beta row, including the append-only usage ledger.
    row = seed_model(django_apps.get_model(label), firms["beta"])
    _assert_owned(row, firms["beta"])
    statement = sql.SQL("DELETE FROM {} WHERE id = %s").format(
        sql.Identifier(row._meta.db_table)
    )

    # When alpha attempts a raw DELETE (no cascading ORM collector).
    with tenant_context(firms["alpha"].tenant.pk):
        if label == "tenants.UsageRecord":
            with (
                pytest.raises(ProgrammingError) as error,
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement, [str(row.pk)])
            assert getattr(error.value.__cause__, "sqlstate", None) == "42501"
        else:
            with connection.cursor() as cursor:
                cursor.execute(statement, [str(row.pk)])
                assert cursor.rowcount == 0

    # Then neither row hiding nor the append-only privilege gate removed data.
    _assert_owned(row, firms["beta"])


def _portal_statement(cursor: CursorWrapper, operation: str, row: models.Model) -> None:
    """Attempt real SQL reads/writes against a known non-empty target."""
    table = sql.Identifier(row._meta.db_table)
    statements = {
        "SELECT": sql.SQL("SELECT * FROM {} WHERE id = %s"),
        "UPDATE": sql.SQL("UPDATE {} SET id = id WHERE id = %s"),
        "DELETE": sql.SQL("DELETE FROM {} WHERE id = %s"),
    }
    if operation == "INSERT":
        _manager(type(row)).create(**_copy_values(row))
    else:
        cursor.execute(statements[operation].format(table), [str(row.pk)])


@pytest.mark.parametrize("label", PORTAL_NONE_LABELS)
@pytest.mark.parametrize("operation", ["SELECT", "INSERT", "UPDATE", "DELETE"])
def test_portal_none_refuses_real_read_and_write(
    label: str, operation: str, firms: dict[str, SeedContext]
) -> None:
    # Given an owning-positive runtime row, not an empty table.
    beta = firms["beta"]
    row = seed_model(django_apps.get_model(label), beta)
    _assert_owned(row, beta)

    # When the portal, even with the correct tenant/client GUCs, accesses it.
    with (
        tenant_context(beta.tenant.pk),
        client_context(beta.client.pk),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE app_portal")
        with pytest.raises(ProgrammingError) as error:
            _portal_statement(cursor, operation, row)
        transaction.set_rollback(True)

    # Then the privilege denial is explicit, and no portal write survived.
    assert getattr(error.value.__cause__, "sqlstate", None) == "42501"
    _assert_owned(row, beta)
