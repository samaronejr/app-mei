"""Prove the successor graph at the database boundary, before activation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from tests.cycles.approval_fixtures import SIGNAL_TIMEOUT
from tests.cycles.fiscal_review_fixtures import (
    blank_review_notes,
    constraint_name,
    insert_review_edge,
    insert_review_rows,
    other_cycle,
    row_image,
)
from tests.cycles.test_fiscal_document_review import record, world
from tests.integrations.capture_fixtures import model
from tests.isolation.axorim_seed import SeedContext, make_seed_context, seed_model
from tests.support_threads import run_in_thread_as_runtime

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.integrations.capture_fixtures import CaptureWorld

__all__ = ["world"]
pytestmark = pytest.mark.django_db(transaction=True)
CHAIN = "fiscaldocumentreview_supersedes_chain_fk"
UNIQUE = "fiscaldocumentreview_revision_uniq"
REASON = "fiscaldocumentreview_successor_reason"


def test_storage_appends_two_ordered_edges_and_keeps_the_original(
    world: CaptureWorld,
) -> None:
    root = record(world, world.ingest(), "rejeitado", note="")
    first, second = uuid4(), uuid4()
    with tenant_context(world.tenant_id), connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(
            cursor, type(root)._meta.db_table
        )
        assert "supersedes_id" in {column.name for column in columns}
        before = row_image(root.pk)
        root.note = "Synthetic correction."
        insert_review_rows(root, [(first, root.pk), (second, first)])
        assert row_image(root.pk) == before
        reviews = type(root).objects
        assert dict(reviews.values_list("pk", "supersedes_id")) == {
            root.pk: None,
            first: root.pk,
            second: first,
        }
        root.cycle_id = other_cycle(world).pk
        separate = insert_review_edge(root, None)
        assert reviews.get(pk=separate).supersedes_id is None


def _foreign_predecessor(world: CaptureWorld, variant: str) -> UUID:
    if variant == "tenant":
        ctx = make_seed_context("foreign-review", "foreign-a")
    else:
        with tenant_context(world.tenant_id):
            client = ClientCompany.objects.create(
                tenant_id=world.tenant_id,
                cnpj="12345678000195",
                legal_name="Second client",
                status="active",
            )
            ctx = SeedContext(
                world.setup.graph.tenant, client, world.setup.graph.actor, "b"
            )
    with tenant_context(ctx.tenant.pk):
        predecessor = seed_model(model("FiscalDocumentReview", app="cycles"), ctx).pk
    assert isinstance(predecessor, UUID)
    return predecessor


@pytest.mark.parametrize(
    ("variant", "constraint"),
    [
        ("root", UNIQUE),
        ("fork", UNIQUE),
        ("self", "fiscaldocumentreview_not_self"),
        ("cycle", CHAIN),
        ("revision", CHAIN),
        ("missing", CHAIN),
        ("tenant", CHAIN),
        ("client", CHAIN),
    ],
)
def test_database_rejects_malformed_review_edges(
    world: CaptureWorld, variant: str, constraint: str
) -> None:
    root = record(world, world.ingest(), "rejeitado")
    key, predecessor = uuid4(), root.pk
    if variant == "root":
        predecessor = None
    elif variant == "self":
        predecessor = key
    elif variant == "cycle":
        root.cycle_id = other_cycle(world).pk
    elif variant == "revision":
        root.revision_id = world.ingest(version=2).current_revision_id
    elif variant == "missing":
        predecessor = uuid4()
    elif variant in {"tenant", "client"}:
        predecessor = _foreign_predecessor(world, variant)
    with tenant_context(world.tenant_id):
        if variant == "fork":
            insert_review_edge(root, root.pk)
        try:
            with transaction.atomic():
                insert_review_rows(root, [(key, predecessor)])
        except IntegrityError as refused:
            actual = constraint_name(refused)
        else:
            actual = None
        assert actual == constraint
        assert not type(root).objects.filter(pk=key).exists()


def test_database_rejects_every_blank_review_reason(world: CaptureWorld) -> None:
    root = record(world, world.ingest(), "rejeitado", note="")
    assert root.note == ""
    results = {}
    with tenant_context(world.tenant_id):
        for note in blank_review_notes():
            root.note = note
            try:
                with transaction.atomic():
                    insert_review_edge(root, root.pk)
                    transaction.set_rollback(True)
            except IntegrityError as error:
                results[ascii(note)] = constraint_name(error)
            else:
                results[ascii(note)] = None
        assert results == dict.fromkeys(map(ascii, blank_review_notes()), REASON)
        root.note = "\u00a0 Motivo válido. \u202f"
        successor = insert_review_edge(root, root.pk)
        assert type(root).objects.get(pk=successor).note == root.note


@pytest.mark.parametrize("operation", ["update", "delete", "repoint"])
@pytest.mark.parametrize("successor", [False, True])
def test_owner_cannot_mutate_root_or_successor(
    world: CaptureWorld, operation: str, successor: bool
) -> None:
    root = record(world, world.ingest(), "rejeitado")
    with tenant_context(world.tenant_id):
        target = insert_review_edge(root, root.pk) if successor else root.pk
    statements = {
        "update": "UPDATE cycles_fiscaldocumentreview "
        "SET note = 'rewritten' WHERE id = %s",
        "delete": "DELETE FROM cycles_fiscaldocumentreview WHERE id = %s",
        "repoint": "UPDATE cycles_fiscaldocumentreview "
        "SET supersedes_id = NULL WHERE id = %s",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege('app_runtime', "
            "'cycles_fiscaldocumentreview', 'UPDATE'), "
            "has_table_privilege('app_runtime', "
            "'cycles_fiscaldocumentreview', 'DELETE'), "
            "has_column_privilege('app_portal', 'cycles_fiscaldocumentreview', "
            "'supersedes_id', 'SELECT')"
        )
        assert cursor.fetchone() == (False, False, False)
        cursor.execute("RESET ROLE")
        try:
            cursor.execute(
                "SELECT current_user, pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'cycles_fiscaldocumentreview'::regclass"
            )
            login, owner = cursor.fetchone()
            assert login == owner
            assert login != "app_runtime"
            before = row_image(target)
            with pytest.raises(DatabaseError) as refused, transaction.atomic():
                cursor.execute(statements[operation], [target])
            assert getattr(refused.value.__cause__, "sqlstate", None) == "42501"
            assert row_image(target) == before
        finally:
            cursor.execute("SET ROLE app_runtime")


@pytest.mark.parametrize("length", [2, 3, 8])
def test_database_refuses_a_single_statement_cyclic_component(
    world: CaptureWorld, length: int
) -> None:
    root = record(world, world.ingest(), "rejeitado")
    if length == 2:
        root.cycle_id = other_cycle(world).pk
    keys = [uuid4() for _ in range(length)]
    with tenant_context(world.tenant_id):
        try:
            with transaction.atomic():
                insert_review_rows(
                    root, list(zip(keys, keys[1:] + keys[:1], strict=True))
                )
        except IntegrityError as refused:
            actual = constraint_name(refused)
        else:
            actual = None
        assert actual == CHAIN
        assert not type(root).objects.filter(pk__in=keys).exists()


def test_deferred_constraints_do_not_allow_a_forward_edge(world: CaptureWorld) -> None:
    root = record(world, world.ingest(), "rejeitado")
    first, second = uuid4(), uuid4()
    with tenant_context(world.tenant_id), connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        with pytest.raises(IntegrityError) as refused, transaction.atomic():
            insert_review_rows(root, [(first, second)])
        assert constraint_name(refused.value) == CHAIN
        assert not type(root).objects.filter(pk__in=[first, second]).exists()


def signal_statement(prefix: str, reached: Event) -> Callable[..., object]:
    def signal(
        execute: Callable[..., object],
        statement: object,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        if prefix in str(statement):
            reached.set()
        return execute(statement, params, many, context)

    return signal


@pytest.mark.parametrize("edge", ["root", "fork", "mutual"])
def test_concurrent_raw_edges_leave_one_rooted_path(
    world: CaptureWorld, edge: str
) -> None:
    root = record(world, world.ingest(), "rejeitado")
    if edge == "root":
        root.cycle_id = other_cycle(world).pk
    first, second = uuid4(), uuid4()
    before = Event()
    predecessor = root.pk if edge == "fork" else None

    def insert_second() -> None:
        with connection.execute_wrapper(signal_statement("INSERT INTO", before)):
            insert_review_rows(
                root, [(second, first if edge == "mutual" else predecessor)]
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with tenant_context(world.tenant_id):
            if edge == "mutual":
                with (
                    pytest.raises(IntegrityError) as first_refused,
                    transaction.atomic(),
                ):
                    insert_review_rows(root, [(first, second)])
                assert constraint_name(first_refused.value) == CHAIN
            else:
                insert_review_rows(root, [(first, predecessor)])
            loser = executor.submit(
                run_in_thread_as_runtime, world.tenant_id, insert_second
            )
            assert before.wait(SIGNAL_TIMEOUT)
        with pytest.raises(IntegrityError) as refused:
            loser.result(SIGNAL_TIMEOUT)
        assert constraint_name(refused.value) == (CHAIN if edge == "mutual" else UNIQUE)
    with tenant_context(world.tenant_id):
        rows = type(root).objects.filter(pk__in=[first, second])
        assert dict(rows.values_list("pk", "supersedes_id")) == (
            {} if edge == "mutual" else {first: predecessor}
        )
