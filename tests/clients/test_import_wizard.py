"""The portfolio import (AXR1-007, plan S7.1 / M2-I): plan, commit, roll back.

Acceptance criteria of todo 7, each pinned here:

* a 20-row file with 2 invalid rows and 1 duplicate plans as 17/2/1;
* commit creates the 17 with the batch id as provenance (through the TenantTask);
* rollback deletes them while nothing depends on them;
* once an assignment, a contact or a portal invite exists on an imported client,
  rollback is refused listing those dependents — through a registry later todos
  extend;
* a CNPJ another firm holds imports fine here (no leak, no cross-tenant dedupe);
* `=cmd` names are defused in the error CSV;
* a 5,001-row file answers 422 "limite de 5.000 linhas" and creates nothing.

Plus the review contract: stale versions from two callers conflict with no writes,
an unassigned staff accountant is refused with no rows written, and each
correctness filter has a test that fails when the filter is removed.
"""

import csv
import io
import zipfile
from collections.abc import Sequence
from datetime import date
from http import HTTPStatus
from typing import Any, Final
from uuid import UUID

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.urls import reverse
from openpyxl import Workbook

from apps.accounts.invites import issue_client_invite
from apps.accounts.models import User
from apps.audit.models import AuditAction, Event
from apps.clients import importing
from apps.clients.importing import (
    MAX_ROWS,
    ImportConflictError,
    ImportFileError,
    MappingIncompleteError,
    RollbackRefusedError,
    build_error_csv,
    commit_import,
    plan_import,
    queue_commit,
    rollback_import,
)
from apps.clients.models import (
    ClientAssignment,
    ClientCompany,
    ClientContact,
    ImportBatch,
    ImportRow,
    ImportStatus,
    OnboardingItem,
    RowOutcome,
)
from apps.clients.tasks import commit_import_task
from apps.core.tenancy import tenant_context
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation
from apps.obligations.models.operations import ObligationStatus
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.isolation.rolecheck import assert_isolated_role
from tests.ui.factories import Firm, add_client, client_base, make_cnpj, make_firm

pytestmark = pytest.mark.django_db(transaction=True)

SEED: Final = "importacao"
HEADER: Final = ("CNPJ", "Razão social", "UF")
FORMULA_NAME: Final = "=cmd|' /c calc'!A1"


def cnpj(index: int, seed: str = SEED) -> str:
    return make_cnpj(client_base(index, seed))


def csv_upload(
    rows: Sequence[Sequence[str]],
    *,
    header: Sequence[str] = HEADER,
    name: str = "clientes.csv",
    encoding: str = "utf-8",
    prefix: bytes = b"",
) -> SimpleUploadedFile:
    lines = [";".join(header), *(";".join(row) for row in rows)]
    return SimpleUploadedFile(
        name,
        prefix + "\n".join(lines).encode(encoding),
        content_type="text/csv",
    )


def twenty_rows(duplicate: str) -> list[tuple[str, str, str]]:
    """17 new clients, 2 invalid rows, 1 CNPJ already in the firm."""
    rows = [(cnpj(i), f"Cliente Importado {i} MEI", "SP") for i in range(17)]
    rows.insert(5, ("12345678000100", FORMULA_NAME, "SP"))  # wrong check digits
    rows.insert(11, (cnpj(40), "", "RJ"))  # no legal name
    rows.append((duplicate, "Já Existe MEI", "MG"))
    return rows


@pytest.fixture
def tenant() -> Tenant:
    return Tenant.objects.create(name="Escritorio Alfa", slug="alfa")


@pytest.fixture
def other_tenant() -> Tenant:
    return Tenant.objects.create(name="Escritorio Beta", slug="beta")


def _member(tenant: Tenant, email: str, role: str) -> User:
    user = User.objects.create_user(email=email)
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return user


@pytest.fixture
def owner(tenant: Tenant) -> User:
    return _member(tenant, "dona@alfa.test", TenantRole.OWNER)


@pytest.fixture
def staff(tenant: Tenant) -> User:
    return _member(tenant, "contador@alfa.test", TenantRole.STAFF_ACCOUNTANT)


@pytest.fixture
def existing(tenant: Tenant) -> ClientCompany:
    with tenant_context(tenant.id):
        return ClientCompany.objects.create(
            tenant=tenant,
            legal_name="Já Existe MEI",
            cnpj=cnpj(90),
        )


@pytest.fixture
def planned(
    tenant: Tenant,
    owner: User,
    existing: ClientCompany,
) -> importing.ImportPlan:
    with tenant_context(tenant.id):
        return plan_import(owner, csv_upload(twenty_rows(existing.cnpj)), None)


def _commit(tenant: Tenant, owner: User, plan: importing.ImportPlan) -> None:
    with tenant_context(tenant.id):
        commit_import(owner, plan.batch_id, expected_version=plan.version)


def _version(tenant: Tenant, batch_id: UUID) -> int:
    with tenant_context(tenant.id):
        return int(ImportBatch.objects.get(pk=batch_id).version)


def _imported(tenant: Tenant, batch_id: UUID) -> list[ClientCompany]:
    with tenant_context(tenant.id):
        return list(ClientCompany.objects.filter(import_batch_id=batch_id))


# --- plan -----------------------------------------------------------------------


def test_the_plan_of_twenty_rows_shows_seventeen_two_and_one(
    tenant: Tenant,
    planned: importing.ImportPlan,
) -> None:
    assert_isolated_role()
    assert (planned.total, planned.created, planned.invalid, planned.duplicate) == (
        20,
        17,
        2,
        1,
    )
    with tenant_context(tenant.id):
        outcomes = list(
            ImportRow.objects.filter(batch_id=planned.batch_id)
            .order_by("row_number")
            .values_list("outcome", flat=True),
        )
        # The dry run moves nothing in the portfolio.
        assert ClientCompany.objects.count() == 1
        batch = ImportBatch.objects.get(pk=planned.batch_id)
    assert outcomes.count(RowOutcome.CREATED) == 17
    assert outcomes.count(RowOutcome.INVALID) == 2
    assert outcomes.count(RowOutcome.DUPLICATE_SKIPPED) == 1
    assert batch.status == ImportStatus.PLANNED
    assert batch.mapping == {
        "cnpj": "CNPJ",
        "legal_name": "Razão social",
        "state": "UF",
    }


def test_a_cnpj_repeated_inside_the_file_is_a_duplicate_of_its_first_row(
    tenant: Tenant,
    owner: User,
) -> None:
    rows = [(cnpj(1), "Primeira", "SP"), (cnpj(1), "Segunda", "SP")]
    with tenant_context(tenant.id):
        plan = plan_import(owner, csv_upload(rows), None)
        second = ImportRow.objects.get(batch_id=plan.batch_id, row_number=3)
    assert (plan.created, plan.duplicate) == (1, 1)
    assert second.outcome == RowOutcome.DUPLICATE_SKIPPED
    assert "linha 2" in second.errors[0]


# --- commit ---------------------------------------------------------------------


def test_the_commit_task_creates_the_seventeen_with_the_batch_id(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
) -> None:
    assert_isolated_role()
    with tenant_context(tenant.id):
        queued = queue_commit(owner, planned.batch_id, expected_version=planned.version)

    # Through the TenantTask, exactly as the worker runs it: no ambient context.
    result = commit_import_task.apply(
        args=[str(tenant.id), str(planned.batch_id), str(owner.pk), queued.version],
    ).get()

    assert result["status"] == ImportStatus.COMMITTED
    assert result["created"] == 17
    created = _imported(tenant, planned.batch_id)
    assert len(created) == 17
    assert {client.import_batch_id for client in created} == {planned.batch_id}
    with tenant_context(tenant.id):
        batch = ImportBatch.objects.get(pk=planned.batch_id)
        linked = ImportRow.objects.filter(
            batch_id=planned.batch_id,
            client__isnull=False,
        ).count()
        assert Event.objects.filter(action=AuditAction.IMPORT_COMMITTED).count() == 1
    assert batch.status == ImportStatus.COMMITTED
    assert batch.committed_count == 17
    assert linked == 17


def test_the_commit_view_enqueues_the_task_after_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firm = _flagged_firm("gama")
    sent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(commit_import_task, "delay", lambda *args: sent.append(args))
    browser = firm.as_owner()
    response = browser.post(
        reverse("client-import"),
        {"file": csv_upload([(cnpj(1, "gama"), "Um MEI", "SP")])},
    )
    assert response.status_code == HTTPStatus.FOUND
    with tenant_context(firm.tenant.id):
        batch = ImportBatch.objects.get()

    response = browser.post(
        reverse("client-import-commit", args=[batch.pk]),
        {"version": batch.version},
    )

    assert response.status_code == HTTPStatus.FOUND
    assert sent == [(str(firm.tenant.id), str(batch.pk), str(firm.owner.pk), 2)]
    commit_import_task.apply(args=list(sent[0])).get()
    assert len(_imported(firm.tenant, batch.pk)) == 1


def test_a_crashed_commit_task_writes_nothing_and_hands_the_batch_back(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real, calls = importing._create_client, []

    def crash_on_third(batch: ImportBatch, row: ImportRow) -> ClientCompany:
        calls.append(row.row_number)
        if len(calls) == 3:
            msg = "worker lost mid-commit"
            raise RuntimeError(msg)
        return real(batch, row)

    with tenant_context(tenant.id):
        queued = queue_commit(owner, planned.batch_id, expected_version=planned.version)
    monkeypatch.setattr(importing, "_create_client", crash_on_third)
    args = [str(tenant.id), str(planned.batch_id), str(owner.pk), queued.version]

    crashed = commit_import_task.apply(args=args).get()

    assert crashed["status"] == "failed"
    assert _imported(tenant, planned.batch_id) == []
    with tenant_context(tenant.id):
        batch = ImportBatch.objects.get(pk=planned.batch_id)
    assert (batch.status, batch.version) == (ImportStatus.PLANNED, queued.version + 1)

    monkeypatch.setattr(importing, "_create_client", real)
    with tenant_context(tenant.id):
        again = queue_commit(owner, batch.pk, expected_version=batch.version)
    args[3] = again.version
    assert commit_import_task.apply(args=args).get()["created"] == 17
    # A redelivery of the same message is a benign no-op, not a failure.
    assert commit_import_task.apply(args=args).get()["status"] == "conflict"
    assert len(_imported(tenant, planned.batch_id)) == 17


def test_queueing_a_commit_is_an_audited_state_change(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
) -> None:
    with tenant_context(tenant.id):
        before = Event.objects.count()
        queued = queue_commit(owner, planned.batch_id, expected_version=planned.version)
        events = list(Event.objects.order_by("created_at")[before:])
    assert (queued.status, queued.version) == (ImportStatus.QUEUED, 2)
    assert [(e.action, e.object_id) for e in events] == [
        ("import_queued", str(planned.batch_id)),
    ]
    assert events[0].metadata == {"from": "planned", "version": 2}


def test_handing_back_a_crashed_commit_is_an_audited_state_change(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(batch: ImportBatch, row: ImportRow) -> ClientCompany:
        msg = "worker lost mid-commit"
        raise RuntimeError(msg)

    with tenant_context(tenant.id):
        queued = queue_commit(owner, planned.batch_id, expected_version=planned.version)
    monkeypatch.setattr(importing, "_create_client", crash)
    args = [str(tenant.id), str(planned.batch_id), str(owner.pk), queued.version]

    assert commit_import_task.apply(args=args).get()["status"] == "failed"

    with tenant_context(tenant.id):
        failed = list(Event.objects.filter(action="import_commit_failed"))
        batch = ImportBatch.objects.get(pk=planned.batch_id)
    assert batch.status == ImportStatus.PLANNED
    assert [(e.object_id, e.actor_id) for e in failed] == [
        (str(planned.batch_id), owner.pk),
    ]
    assert failed[0].metadata == {
        "from": "queued",
        "to": "planned",
        "version": batch.version,
        "error": "RuntimeError",
    }


def test_a_cnpj_registered_after_the_plan_is_skipped_at_commit(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
) -> None:
    with tenant_context(tenant.id):
        ClientCompany.objects.create(
            tenant=tenant, legal_name="Chegou Antes", cnpj=cnpj(3)
        )
        result = commit_import(
            owner, planned.batch_id, expected_version=planned.version
        )
        batch = ImportBatch.objects.get(pk=planned.batch_id)
    assert (result.created, result.skipped) == (16, 1)
    assert (batch.planned_created, batch.planned_duplicate) == (16, 2)


def test_a_row_that_fails_to_write_leaves_the_batch_partial_and_retry_finishes_it(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = importing._create_client
    target = cnpj(7)

    def flaky(batch: ImportBatch, row: ImportRow) -> ClientCompany:
        if row.cnpj == target:
            msg = "simulated"
            raise IntegrityError(msg)
        return real(batch, row)

    monkeypatch.setattr(importing, "_create_client", flaky)
    with tenant_context(tenant.id):
        first = commit_import(owner, planned.batch_id, expected_version=planned.version)
        batch = ImportBatch.objects.get(pk=planned.batch_id)
    assert (first.status, first.created, first.failed) == (ImportStatus.PARTIAL, 16, 1)
    with tenant_context(tenant.id):
        assert "Falha ao gravar" in build_error_csv(batch)

    monkeypatch.setattr(importing, "_create_client", real)
    with tenant_context(tenant.id):
        queued = queue_commit(owner, batch.pk, expected_version=batch.version)
        retry = commit_import(owner, batch.pk, expected_version=queued.version)
        rows = list(
            ImportRow.objects.filter(
                batch=batch, outcome=RowOutcome.CREATED
            ).values_list(
                "client_id",
                flat=True,
            ),
        )
    # Only the failed row is attempted again: the filter on client IS NULL is what
    # keeps the sixteen already-created rows from being re-read as duplicates.
    assert (retry.status, retry.created, retry.failed, retry.skipped) == (
        ImportStatus.COMMITTED,
        1,
        0,
        0,
    )
    assert len(rows) == 17
    assert None not in rows
    assert len(_imported(tenant, batch.pk)) == 17


# --- rollback -------------------------------------------------------------------


def test_rollback_deletes_the_batch_clients_when_nothing_depends_on_them(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
    existing: ClientCompany,
) -> None:
    assert_isolated_role()
    _commit(tenant, owner, planned)
    with tenant_context(tenant.id):
        # Generated calendar rows are not dependents; they cascade with the client.
        for client in ClientCompany.objects.filter(import_batch_id=planned.batch_id)[
            :2
        ]:
            generate_das_calendar(client, starting_from=date(2026, 1, 1))
        assert Obligation.objects.exists()
        result = rollback_import(
            owner,
            planned.batch_id,
            expected_version=_version(tenant, planned.batch_id),
        )
        batch = ImportBatch.objects.get(pk=planned.batch_id)
        rows = ImportRow.objects.filter(batch=batch).count()
        remaining = list(ClientCompany.objects.values_list("pk", flat=True))
    assert result.deleted == 17
    assert batch.status == ImportStatus.ROLLED_BACK
    # Rows stay as history; the pre-existing client is untouched.
    assert rows == 20
    assert remaining == [existing.pk]


def test_rollback_is_refused_listing_assignments_contacts_and_invites(
    tenant: Tenant,
    owner: User,
    staff: User,
    planned: importing.ImportPlan,
) -> None:
    _commit(tenant, owner, planned)
    first, second, third = sorted(
        _imported(tenant, planned.batch_id),
        key=lambda client: client.cnpj,
    )[:3]
    with tenant_context(tenant.id):
        ClientAssignment.objects.create(tenant=tenant, client=first, user=staff)
        ClientContact.objects.create(tenant=tenant, client=second, name="Maria")
        issue_client_invite(
            actor=owner,
            tenant=tenant,
            client=third,
            email="maria@cliente.test",
            role=TenantRole.CLIENT_OWNER,
        )
    version = _version(tenant, planned.batch_id)

    with tenant_context(tenant.id):
        with pytest.raises(RollbackRefusedError) as refusal:
            rollback_import(owner, planned.batch_id, expected_version=version)
        remaining = ClientCompany.objects.filter(import_batch_id=planned.batch_id)
        batch = ImportBatch.objects.get(pk=planned.batch_id)
        rolled_back_events = Event.objects.filter(
            action=AuditAction.IMPORT_ROLLED_BACK,
        ).count()

        found = {
            (dep.kind, dep.client_id, dep.count) for dep in refusal.value.dependents
        }
        assert found == {
            ("clients.assignment", first.pk, 1),
            ("clients.contact", second.pk, 1),
            ("tenants.portal_invite", third.pk, 1),
        }
        assert remaining.count() == 17
    assert (batch.status, batch.version) == (ImportStatus.COMMITTED, version)
    assert rolled_back_events == 0


def test_a_settled_obligation_blocks_rollback_and_a_generated_one_does_not(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
) -> None:
    _commit(tenant, owner, planned)
    with tenant_context(tenant.id):
        client = ClientCompany.objects.filter(import_batch_id=planned.batch_id).first()
        assert client is not None
        generate_das_calendar(client, starting_from=date(2026, 1, 1))
        ids = [client.pk]
        assert importing.find_dependents(tenant.id, ids) == []
        Obligation.objects.filter(client=client).update(status=ObligationStatus.PAID)
        kinds = {dep.kind for dep in importing.find_dependents(tenant.id, ids)}
    assert kinds == {"obligations.settled"}


def test_onboarding_items_block_rollback_only_once_someone_worked_them(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
) -> None:
    _commit(tenant, owner, planned)
    with tenant_context(tenant.id):
        client = ClientCompany.objects.filter(import_batch_id=planned.batch_id).first()
        assert client is not None
        ids = [client.pk]
        # The signal attaches a pending checklist to every new client.
        assert client.onboarding_items.exists()
        assert importing.find_dependents(tenant.id, ids) == []
        item = client.onboarding_items.first()
        assert item is not None
        item.status = "done"
        item.save(update_fields=["status"])
        kinds = {dep.kind for dep in importing.find_dependents(tenant.id, ids)}
    assert kinds == {"clients.onboarding_progress"}


def test_the_dependents_registry_is_extensible_by_later_modules(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _commit(tenant, owner, planned)
    # A copy, restored by monkeypatch: no test leaves a kind registered for the next.
    monkeypatch.setattr(importing, "_DEPENDENTS", dict(importing._DEPENDENTS))
    importing.register_rollback_dependent(
        "cycles.servicecycle",
        "ciclos de serviço",
        lambda tenant_id, ids: OnboardingItem.objects.filter(
            tenant_id=tenant_id,
            client_id__in=ids,
        ),
    )
    with pytest.raises(ValueError, match="already registered"):
        importing.register_rollback_dependent(
            "cycles.servicecycle",
            "x",
            lambda tenant_id, ids: ClientCompany.objects.none(),
        )
    with tenant_context(tenant.id), pytest.raises(RollbackRefusedError) as refusal:
        rollback_import(
            owner,
            planned.batch_id,
            expected_version=_version(tenant, planned.batch_id),
        )
    assert {dep.kind for dep in refusal.value.dependents} == {"cycles.servicecycle"}


# --- concurrency and authorization -----------------------------------------------


def test_two_callers_with_the_same_version_cannot_both_commit(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
) -> None:
    _commit(tenant, owner, planned)
    with tenant_context(tenant.id):
        with pytest.raises(ImportConflictError):
            commit_import(owner, planned.batch_id, expected_version=planned.version)
        assert (
            ClientCompany.objects.filter(import_batch_id=planned.batch_id).count() == 17
        )
        assert Event.objects.filter(action=AuditAction.IMPORT_COMMITTED).count() == 1


def test_a_stale_rollback_conflicts_and_deletes_nothing(
    tenant: Tenant,
    owner: User,
    planned: importing.ImportPlan,
) -> None:
    _commit(tenant, owner, planned)
    with tenant_context(tenant.id), pytest.raises(ImportConflictError):
        rollback_import(owner, planned.batch_id, expected_version=planned.version)
    assert len(_imported(tenant, planned.batch_id)) == 17


def test_a_staff_accountant_may_not_plan_commit_or_roll_back(
    tenant: Tenant,
    owner: User,
    staff: User,
    existing: ClientCompany,
) -> None:
    assert_isolated_role()
    with tenant_context(tenant.id):
        with pytest.raises(PermissionDenied):
            plan_import(staff, csv_upload(twenty_rows(existing.cnpj)), None)
        assert ImportBatch.objects.count() == 0
        plan = plan_import(owner, csv_upload(twenty_rows(existing.cnpj)), None)
        with pytest.raises(PermissionDenied):
            commit_import(staff, plan.batch_id, expected_version=plan.version)
        assert ClientCompany.objects.count() == 1
        commit_import(owner, plan.batch_id, expected_version=plan.version)
        with pytest.raises(PermissionDenied):
            rollback_import(staff, plan.batch_id, expected_version=plan.version + 1)
        assert ClientCompany.objects.count() == 18


# --- tenancy --------------------------------------------------------------------


def test_a_cnpj_held_by_another_firm_imports_without_dedupe_or_leak(
    tenant: Tenant,
    other_tenant: Tenant,
    owner: User,
) -> None:
    assert_isolated_role()
    shared = cnpj(5)
    with tenant_context(other_tenant.id):
        theirs = ClientCompany.objects.create(
            tenant=other_tenant,
            legal_name="Cliente do Outro Escritório",
            cnpj=shared,
        )
    with tenant_context(tenant.id):
        plan = plan_import(owner, csv_upload([(shared, "Nosso Cliente", "SP")]), None)
        row = ImportRow.objects.get(batch_id=plan.batch_id)
        commit_import(owner, plan.batch_id, expected_version=plan.version)
        ours = ClientCompany.objects.get(cnpj=shared)
    assert (plan.created, plan.duplicate) == (1, 0)
    assert row.errors == []
    assert ours.pk != theirs.pk
    with tenant_context(other_tenant.id):
        untouched = ClientCompany.objects.get(pk=theirs.pk)
    assert (untouched.legal_name, untouched.import_batch_id) == (
        "Cliente do Outro Escritório",
        None,
    )


# --- error report ---------------------------------------------------------------


def test_formula_names_are_defused_in_the_error_csv(
    tenant: Tenant,
    planned: importing.ImportPlan,
) -> None:
    with tenant_context(tenant.id):
        report = build_error_csv(ImportBatch.objects.get(pk=planned.batch_id))
    rows = list(csv.reader(io.StringIO(report)))
    assert rows[0] == ["linha", "cnpj", "razao_social", "resultado", "motivo"]
    names = [row[2] for row in rows[1:]]
    assert "'" + FORMULA_NAME in names
    assert FORMULA_NAME not in names
    # Every data cell is either text a spreadsheet will not execute or empty.
    for row in rows[1:]:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@")), cell


def test_a_formula_in_the_cnpj_column_is_defused_too(
    tenant: Tenant,
    owner: User,
) -> None:
    with tenant_context(tenant.id):
        plan = plan_import(owner, csv_upload([("=1+1", "@SUM(A1)", "SP")]), None)
        report = build_error_csv(ImportBatch.objects.get(pk=plan.batch_id))
    data = list(csv.reader(io.StringIO(report)))[1]
    assert data[1] == "'=1+1"
    assert data[2] == "'@SUM(A1)"


# --- file limits and parsing -----------------------------------------------------


def _flagged_firm(slug: str) -> Firm:
    firm = make_firm(slug)
    firm.tenant.features = ["import_r1"]
    firm.tenant.save(update_fields=["features"])
    return firm


def test_a_5001_row_file_is_refused_with_422_and_creates_nothing() -> None:
    firm = _flagged_firm("delta")
    add_client(firm, legal_name="Existente MEI", base=client_base(0, "delta"))
    rows = [(f"{index:014d}", f"Linha {index}", "SP") for index in range(MAX_ROWS + 1)]

    response = firm.as_owner().post(
        reverse("client-import"), {"file": csv_upload(rows)}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "limite de 5.000 linhas" in response.content.decode()
    with tenant_context(firm.tenant.id):
        assert ImportBatch.objects.count() == 0
        assert ImportRow.objects.count() == 0
        assert ClientCompany.objects.count() == 1


def test_exactly_5000_rows_are_accepted(tenant: Tenant, owner: User) -> None:
    rows = [(cnpj(1), f"Linha {index}", "SP") for index in range(MAX_ROWS)]
    with tenant_context(tenant.id):
        plan = plan_import(owner, csv_upload(rows), None)
    assert plan.total == MAX_ROWS


def test_a_file_over_5_mib_is_refused(tenant: Tenant, owner: User) -> None:
    big = SimpleUploadedFile("grande.csv", b"CNPJ;Nome\n" + b"x" * (5 * 1024 * 1024))
    with tenant_context(tenant.id), pytest.raises(ImportFileError, match="5 MiB"):
        plan_import(owner, big, None)


def test_unrecognized_headers_ask_for_a_mapping_and_the_choice_is_honoured(
    tenant: Tenant,
    owner: User,
) -> None:
    header = ("Documento", "Empresa X")
    rows = [(cnpj(2), "Mapeada MEI")]
    with tenant_context(tenant.id):
        with pytest.raises(MappingIncompleteError) as asked:
            plan_import(owner, csv_upload(rows, header=header), None)
        assert asked.value.headers == ["Documento", "Empresa X"]
        plan = plan_import(
            owner,
            csv_upload(rows, header=header),
            {"cnpj": "Documento", "legal_name": "Empresa X"},
        )
    assert plan.created == 1


def test_bom_and_windows_1252_files_are_read(tenant: Tenant, owner: User) -> None:
    rows = [(cnpj(3), "Padaria São João MEI", "SP")]
    with tenant_context(tenant.id):
        bom = plan_import(owner, csv_upload(rows, prefix=b"\xef\xbb\xbf"), None)
        latin = plan_import(owner, csv_upload(rows, encoding="cp1252"), None)
        names = list(
            ImportRow.objects.filter(
                batch_id__in=[bom.batch_id, latin.batch_id]
            ).values_list("values__legal_name", flat=True),
        )
    assert (bom.created, latin.created) == (1, 1)
    assert names == ["Padaria São João MEI", "Padaria São João MEI"]


@pytest.mark.parametrize(
    ("name", "payload", "message"),
    [
        ("lixo.csv", b"CNPJ;Nome\n\x81\x8d\x00\x90garbage", "ler o texto"),
        ("vazio.csv", b"   \n\n", "vazio"),
        ("planilha.xlsx", b"not a zip at all", "XLSX"),
        ("zip.xlsx", b"PK\x03\x04broken", "XLSX"),
        ("antiga.xls", b"\xd0\xcf\x11\xe0", ".xls"),
        ("so-cabecalho.csv", b"CNPJ;Nome\n", "nenhuma linha"),
    ],
)
def test_malformed_files_are_refused_before_anything_is_written(
    tenant: Tenant,
    owner: User,
    name: str,
    payload: bytes,
    message: str,
) -> None:
    with tenant_context(tenant.id):
        with pytest.raises(ImportFileError, match=message):
            plan_import(owner, SimpleUploadedFile(name, payload), None)
        assert ImportBatch.objects.count() == 0


def _xlsx(rows: list[list[object]]) -> SimpleUploadedFile:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return SimpleUploadedFile("clientes.xlsx", buffer.getvalue())


def test_xlsx_is_read_without_evaluating_formulas_and_restores_leading_zeros(
    tenant: Tenant,
    owner: User,
) -> None:
    zero_led = make_cnpj("012345678000")
    upload = _xlsx(
        [
            ["CNPJ", "Razão social", "UF"],
            [int(zero_led), "Zero à Esquerda MEI", "SP"],
            [cnpj(4), '=HYPERLINK("http://evil")', "RJ"],
            [cnpj(6), "Com Fórmula MEI", "=1+1"],
        ],
    )
    with tenant_context(tenant.id):
        plan = plan_import(owner, upload, None)
        rows = {
            row.row_number: row
            for row in ImportRow.objects.filter(batch_id=plan.batch_id)
        }
        report = build_error_csv(ImportBatch.objects.get(pk=plan.batch_id))
    assert rows[2].cnpj == zero_led
    assert rows[2].outcome == RowOutcome.CREATED
    # A formula cell is read as its cached value; openpyxl never computes it, and a
    # never-computed formula reads as empty: no legal name (invalid), no UF (allowed).
    assert rows[3].outcome == RowOutcome.INVALID
    assert rows[3].values["legal_name"] == ""
    assert rows[4].outcome == RowOutcome.CREATED
    assert rows[4].values["state"] == ""
    assert "HYPERLINK" not in report


def test_an_xlsx_whose_shared_strings_inflate_past_the_bound_is_refused(
    tenant: Tenant,
    owner: User,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/sharedStrings.xml", "a" * (6 * 1024 * 1024))
    upload = SimpleUploadedFile("bomba.xlsx", buffer.getvalue())
    assert upload.size is not None
    assert upload.size < 64 * 1024
    with (
        tenant_context(tenant.id),
        pytest.raises(ImportFileError, match="grande demais"),
    ):
        plan_import(owner, upload, None)


# --- the wizard screens -------------------------------------------------------------


def test_the_wizard_is_absent_while_the_flag_is_off() -> None:
    firm = make_firm("epsilon")
    response = firm.as_owner().get(reverse("client-import"))
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_the_wizard_refuses_a_staff_accountant() -> None:
    firm = _flagged_firm("zeta")
    browser = firm.as_accountant()
    response = browser.post(
        reverse("client-import"),
        {"file": csv_upload([(cnpj(1, "zeta"), "Um MEI", "SP")])},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    with tenant_context(firm.tenant.id):
        assert ImportBatch.objects.count() == 0


def test_the_wizard_plans_then_rolls_back_and_explains_a_refusal() -> None:
    firm = _flagged_firm("eta")
    browser = firm.as_owner()
    response = browser.post(
        reverse("client-import"),
        {"file": csv_upload([(cnpj(1, "eta"), "Um MEI", "SP"), ("1", "Ruim", "SP")])},
    )
    assert response.status_code == HTTPStatus.FOUND
    page = browser.get(response["Location"])
    assert page.status_code == HTTPStatus.OK
    content = page.content.decode()
    assert 'data-count="created">1<' in content
    assert 'data-count="invalid">1<' in content
    with tenant_context(firm.tenant.id):
        batch = ImportBatch.objects.get()
        commit_import(firm.owner, batch.pk, expected_version=batch.version)
        batch.refresh_from_db()
        client = ClientCompany.objects.get(import_batch=batch)
        ClientContact.objects.create(tenant=firm.tenant, client=client, name="Ana")

    refused = browser.post(
        reverse("client-import-rollback", args=[batch.pk]),
        {"version": batch.version},
    )
    assert refused.status_code == HTTPStatus.CONFLICT
    assert "contatos cadastrados" in refused.content.decode()

    errors = browser.get(reverse("client-import-errors", args=[batch.pk]))
    assert errors.status_code == HTTPStatus.OK
    assert errors["Content-Type"] == "text/csv; charset=utf-8"
    assert errors.content.startswith(b"\xef\xbb\xbf")


def test_the_upload_screen_asks_for_columns_it_did_not_recognize() -> None:
    firm = _flagged_firm("teta")
    browser = firm.as_owner()
    upload = csv_upload([(cnpj(1, "teta"), "Um MEI")], header=("Doc", "Firma"))
    response = browser.post(reverse("client-import"), {"file": upload})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert 'name="map_cnpj"' in response.content.decode()

    again = csv_upload([(cnpj(1, "teta"), "Um MEI")], header=("Doc", "Firma"))
    response = browser.post(
        reverse("client-import"),
        {"file": again, "map_cnpj": "Doc", "map_legal_name": "Firma"},
    )
    assert response.status_code == HTTPStatus.FOUND
