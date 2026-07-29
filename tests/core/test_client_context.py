"""Client-scoped work must establish the client dimension, and give it back.

The assertions *after* each block are the point of this file, exactly as in
`test_tenant_context.py`. Every block sets the GUC correctly on entry, so an
inside-the-block check passes even when the value leaks out — and a leak is precisely
what would carry one MEI client's scope into the next iteration of a sweep.

PostgreSQL merges a `SET LOCAL` into the parent transaction on `RELEASE SAVEPOINT`
rather than discarding it, so restoring the previous value is explicit rather than
implicit. These tests are what prove that restore actually happens.
"""

import uuid

import pytest
from django.db import connection, transaction

from apps.core.tenancy import (
    CLIENT_GUC,
    MissingClientContext,
    client_context,
    current_client_id,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _read_guc() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT coalesce(current_setting('app.client_id', true), '')")
        row = cursor.fetchone()
    return str(row[0]) if row else ""


def test_the_guc_name_is_the_one_the_policies_compare() -> None:
    # Given the constant the policies were written against
    # Then it is the client GUC, not a copy of the tenant one
    assert CLIENT_GUC == "app.client_id"


def test_the_block_sets_both_layers() -> None:
    # Given a client id
    client = uuid.uuid4()

    # When the context is entered
    with client_context(client) as established:
        # Then both the ORM handle and the database GUC carry it
        assert established == client
        assert current_client_id.get() == client
        assert _read_guc() == str(client)


def test_the_block_gives_the_context_back() -> None:
    # Given no client context
    before = _read_guc()
    assert current_client_id.get() is None

    # When a block runs and exits
    with client_context(uuid.uuid4()):
        pass

    # Then nothing leaked out of it. Asserted here rather than inside the block,
    # because a leak is invisible from within.
    assert _read_guc() == before
    assert current_client_id.get() is None


def test_nesting_restores_the_outer_client() -> None:
    # Given an outer client
    outer = uuid.uuid4()
    inner = uuid.uuid4()

    with client_context(outer):
        assert _read_guc() == str(outer)

        # When an inner block runs and exits
        with client_context(inner):
            assert _read_guc() == str(inner)

        # Then the outer value is back, not the inner one and not empty
        assert _read_guc() == str(outer)
        assert current_client_id.get() == outer


def test_a_sweep_does_not_carry_scope_between_iterations() -> None:
    # Given several clients visited in turn
    clients = [uuid.uuid4() for _ in range(3)]
    seen = []

    # When each is entered and left
    with transaction.atomic():
        for client in clients:
            with client_context(client):
                seen.append(_read_guc())
            # Then each iteration starts clean, which the in-block check above
            # cannot show. This is the case the savepoint merge would break.
            assert _read_guc() == ""

    assert seen == [str(c) for c in clients]


def test_an_exception_inside_the_block_does_not_leak_scope() -> None:
    # Given a block that raises
    before = _read_guc()

    # When the exception propagates
    boom = "boom"
    with pytest.raises(ValueError, match=boom), client_context(uuid.uuid4()):
        raise ValueError(boom)

    # Then the context is still given back. The explicit restore is skipped on this
    # path on purpose; the savepoint rollback is what reverts the setting, and issuing
    # a statement on an aborted transaction would mask the original error.
    assert _read_guc() == before
    assert current_client_id.get() is None


def test_a_missing_client_is_refused_rather_than_silently_empty() -> None:
    # Given no client id
    # When the context is requested anyway
    # Then it raises. Under the fail-closed policy the quiet alternative is zero rows,
    # and a sweep that processes nothing and reports success is worse than a crash.
    with (
        pytest.raises(MissingClientContext, match="requires a client id"),
        client_context(None),
    ):
        pass


def test_the_refusal_does_not_disturb_existing_context() -> None:
    # Given an established client
    client = uuid.uuid4()

    with client_context(client):
        # When a nested block is asked for with no id
        with pytest.raises(MissingClientContext), client_context(None):
            pass

        # Then the outer context survives the refusal
        assert _read_guc() == str(client)
        assert current_client_id.get() == client
