"""Provider receipts stay append-only, even for the tables' owner (AXRM-009, A7.1).

Attempt receipts and effect barriers are the evidence restore replay trusts. Revoking
UPDATE/DELETE from `app_runtime` binds only the web process; the per-table trigger
from `integrations.0003` is what binds the owning roles. Both halves are asserted,
modelled on `tests/audit/test_append_only.py`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

import psycopg
import pytest
from django.db import Error as DatabaseError
from django.db import connection, transaction

from apps.core.tenancy import tenant_context
from apps.integrations.call_models import (
    FirmProviderCallAttempt,
    ProviderCallAttempt,
    ProviderEffectBarrier,
)
from apps.integrations.call_protocol import reserve
from apps.integrations.models import ClientProviderLink, FirmAuthorization
from tests.integrations.test_call_protocol import provider_link, request
from tests.integrations.test_firm_termo_journal import reserve as reserve_termo
from tests.integrations.test_firm_termo_journal import termo
from tests.isolation.rolecheck import assert_isolated_role

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link", "termo"]

RECEIPT_TABLES = (
    "integrations_providercallattempt",
    "integrations_providereffectbarrier",
    "integrations_firmprovidercallattempt",
)


def _seed(table: str, link: ClientProviderLink, firm: FirmAuthorization) -> None:
    if table == "integrations_firmprovidercallattempt":
        submission = reserve_termo(firm)
        with tenant_context(firm.tenant_id):
            FirmProviderCallAttempt.objects.create(
                tenant_id=firm.tenant_id,
                call=submission,
                attempt_id=uuid4(),
                outcome="sucesso",
            )
        return
    call = reserve(link.tenant_id, link.pk, request())
    with tenant_context(link.tenant_id):
        if table == "integrations_providercallattempt":
            ProviderCallAttempt.objects.create(
                tenant_id=link.tenant_id,
                client_id=link.client_id,
                call=call,
                attempt_id=uuid4(),
                outcome="sucesso",
            )
        else:
            ProviderEffectBarrier.objects.create(
                tenant_id=link.tenant_id,
                client_id=link.client_id,
                provider="serpro",
                business_effect_key=sha256(b"effect").hexdigest(),
                request_digest=sha256(b"body").hexdigest(),
                first_start_receipt="provider-journal/synthetic-start.json",
                call=call,
            )


@contextmanager
def _as_owner() -> Iterator[None]:
    """Run as the OWNING role (it holds BYPASSRLS); restore the runtime role after."""
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")


def _owner_row_count(table: str) -> int:
    with _as_owner(), connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _assert_refused(sql: str) -> None:
    """Assert SQLSTATE 42501 (insufficient_privilege), not any database error."""
    with (
        _as_owner(),
        pytest.raises(DatabaseError) as caught,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(sql)
    assert isinstance(caught.value.__cause__, psycopg.errors.InsufficientPrivilege), (
        f"expected insufficient_privilege, got {caught.value.__cause__!r}"
    )


@pytest.mark.parametrize("table", RECEIPT_TABLES)
def test_the_append_only_trigger_exists(table: str) -> None:
    # Given the table's non-internal triggers.
    assert_isolated_role()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = %s AND NOT t.tgisinternal",
            [table],
        )
        triggers = {row[0] for row in cursor.fetchall()}
    # Then the per-table append-only trigger from integrations.0003 is present.
    assert f"{table}_append_only" in triggers


@pytest.mark.parametrize("table", RECEIPT_TABLES)
def test_even_the_table_owner_cannot_update(
    table: str, provider_link: ClientProviderLink, termo: FirmAuthorization
) -> None:
    # Given a written receipt, visible to the owning role (positive control).
    assert_isolated_role()
    _seed(table, provider_link, termo)
    assert _owner_row_count(table) > 0
    # When the owner tries to rewrite it, then the trigger refuses.
    _assert_refused(f"UPDATE {table} SET id = id")  # noqa: S608


@pytest.mark.parametrize("table", RECEIPT_TABLES)
def test_even_the_table_owner_cannot_delete(
    table: str, provider_link: ClientProviderLink, termo: FirmAuthorization
) -> None:
    # Given a written receipt, visible to the owning role (positive control).
    assert_isolated_role()
    _seed(table, provider_link, termo)
    assert _owner_row_count(table) > 0
    # When the owner tries to erase it, then the trigger refuses.
    _assert_refused(f"DELETE FROM {table}")  # noqa: S608


@pytest.mark.parametrize("table", RECEIPT_TABLES)
def test_the_runtime_role_holds_exactly_insert_and_select(table: str) -> None:
    # Given the privileges recorded for the application role.
    assert_isolated_role()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = 'public' AND table_name = %s AND grantee = %s",
            [table, "app_runtime"],
        )
        granted = {row[0] for row in cursor.fetchall()}
    # Then only INSERT and SELECT are held: no UPDATE, DELETE or TRUNCATE.
    assert granted == {"INSERT", "SELECT"}, f"{table} grants: {sorted(granted)}"
