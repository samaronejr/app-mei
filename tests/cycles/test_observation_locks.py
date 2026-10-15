"""B1/B2: subscribe to actual runtime SQL/call boundaries, never supply locks."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles import receipt_observations, scanning
from apps.cycles.errors import IllegalTransition
from apps.cycles.extraction import service as extraction
from apps.cycles.models import EvidenceLink, EvidenceRequest
from apps.cycles.services import review as reviews
from apps.cycles.services.cycle import require_actor
from apps.obligations import observations
from apps.obligations.models import Obligation, SourceObservation
from apps.tenants.models import Membership
from tests.cycles import observation_fixtures
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.document_fixtures import reviewed, scan_clean, submit
from tests.cycles.observation_fixtures import payment_claim, payment_subject
from tests.portal.request_fixtures import PortalSetup
from tests.support_threads import run_in_thread_as_runtime

receipt_case = observation_fixtures.receipt_case
pytestmark = pytest.mark.django_db(transaction=True)
SIGNAL_TIMEOUT = 15
WORKER = ContextVar("observation_test_worker", default="")


def _locking(sql: str, table: str) -> bool:
    return f'FROM "{table}"' in sql and (
        "FOR UPDATE" in sql or "FOR NO KEY UPDATE" in sql
    )


@dataclass(frozen=True, slots=True)
class SQLSignal:
    """Observe the real statement without adding a lock or changing its result."""

    before: Callable[[str], None] | None = None
    after: Callable[[str], None] | None = None

    def __call__(
        self,
        execute: Callable[..., object],
        sql: str,
        params: object,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if self.before is not None:
            self.before(sql)
        result = execute(sql, params, many, context)
        if self.after is not None:
            self.after(sql)
        return result


@dataclass(frozen=True, slots=True)
class ReviewMaterializerOrder:
    """Release each real contender at the lock the first transaction holds.

    Observe the first common fence (client/cycle, formerly intake), rather than
    imposing a transaction protocol from the test.
    """

    materializer_first: bool
    held: Event = field(default_factory=Event)
    contender: Event = field(default_factory=Event)
    review_first_lock: list[str] = field(default_factory=list)

    def review_before(self, sql: str) -> None:
        if self.materializer_first and (
            _locking(sql, "clients_clientcompany")
            or _locking(sql, "cycles_servicecycle")
        ):
            self.contender.set()

    def review_after(self, sql: str) -> None:
        table = (
            "client"
            if _locking(sql, "clients_clientcompany")
            else "intake"
            if _locking(sql, "cycles_documentintake")
            else "cycle"
            if _locking(sql, "cycles_servicecycle")
            else ""
        )
        if not table or self.review_first_lock:
            return
        self.review_first_lock.append(table)
        if self.materializer_first:
            self.contender.set()
        else:
            self.held.set()
            assert self.contender.wait(SIGNAL_TIMEOUT)

    def materializer_before(self, sql: str) -> None:
        outer_lock = (
            _locking(sql, "clients_clientcompany")
            or _locking(sql, "cycles_servicecycle")
            or _locking(sql, "cycles_evidencerequest")
        )
        if (
            not self.materializer_first
            and outer_lock
            and self.review_first_lock in (["client"], ["cycle"])
        ):
            self.contender.set()

    def materializer_after(self, sql: str) -> None:
        outer_lock = (
            _locking(sql, "clients_clientcompany")
            or _locking(sql, "cycles_servicecycle")
            or _locking(sql, "cycles_evidencerequest")
        )
        if not outer_lock:
            return
        if self.materializer_first and not self.held.is_set():
            self.held.set()
            assert self.contender.wait(SIGNAL_TIMEOUT)
        elif not self.materializer_first and self.review_first_lock == ["intake"]:
            self.contender.set()


def _observe[**P, R](
    function: Callable[P, R],
    *,
    before: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
) -> Callable[P, R]:
    def observed(*args: P.args, **kwargs: P.kwargs) -> R:
        if before is not None:
            before()
        result = function(*args, **kwargs)
        if after is not None:
            after()
        return result

    return observed


def _runtime_role() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("app_runtime",)
        cursor.execute("SET LOCAL lock_timeout = '10s'")
        cursor.execute("SET LOCAL statement_timeout = '12s'")


def _phased_runtime[R](function: Callable[[], R]) -> R:
    """Finalizers own their transactions; their external I/O must stay outside."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE app_runtime")
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("app_runtime",)
            cursor.execute("SET lock_timeout = '10s'")
            cursor.execute("SET statement_timeout = '12s'")
        assert not connection.in_atomic_block
        return function()
    finally:
        connections.close_all()


def _decide(setup: PortalSetup, link: EvidenceLink) -> None:
    version = EvidenceRequest.objects.get(pk=link.request_id).version
    reviews.record_review(
        setup.graph.actor,
        link.pk,
        decision="aceito",
        client_reason="",
        corrections=[],
        expected_version=version,
        payment_relevant=True,
    )


@pytest.mark.parametrize(
    "producer", ["staff", "retract", "calendar", "materializer", "review"]
)
def test_actual_producer_evaluates_subject_fence_before_observation_reads(
    receipt_case: PortalSetup, producer: str
) -> None:
    subject = payment_subject(receipt_case)
    head = payment_claim(subject)
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")

    with (
        tenant_context(subject.tenant_id),
        CaptureQueriesContext(connection) as queries,
    ):
        _runtime_role()
        if producer == "staff":
            observations.record_staff_fact(
                receipt_case.graph.actor,
                subject_id=subject.pk,
                kind="payment",
                period=PERIOD,
                expected_head_id=head.pk,
            )
        elif producer == "retract":
            observations.retract_staff_fact(
                receipt_case.graph.actor,
                subject_id=subject.pk,
                kind="payment",
                period=PERIOD,
                expected_head_id=head.pk,
            )
        elif producer == "calendar":
            observations.emit_calendar_assertions([subject])
        elif producer == "materializer":
            receipt_observations.settle_link(link.pk)
        else:
            _decide(receipt_case, link)
    sql = [query["sql"] for query in queries]
    locks = [
        index
        for index, query in enumerate(sql)
        if _locking(query, "obligations_obligation")
    ]
    reads = [
        index
        for index, query in enumerate(sql)
        if 'FROM "obligations_sourceobservation"' in query
    ]
    assert locks, (producer, sql)
    assert reads, (producer, sql)
    assert locks[0] < reads[0], (producer, sql)
    assert 'ORDER BY "obligations_obligation"."id" ASC' in sql[locks[0]]


def test_two_actual_staff_corrections_cannot_fork_one_predecessor(
    receipt_case: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = payment_subject(receipt_case)
    head = payment_claim(subject)
    first_read, rival_progress = Event(), Event()

    def after_head() -> None:
        if WORKER.get() == "first":
            first_read.set()
            assert rival_progress.wait(SIGNAL_TIMEOUT)
        else:
            rival_progress.set()

    def before_rival_lock(sql: str) -> None:
        if "FOR UPDATE" in sql or "FOR NO KEY UPDATE" in sql:
            rival_progress.set()

    monkeypatch.setattr(
        observations,
        "_expected_head",
        _observe(observations._expected_head, after=after_head),
    )

    def correct(label: str) -> str:
        WORKER.set(label)
        _runtime_role()
        signal = SQLSignal(before=before_rival_lock if label == "second" else None)
        try:
            with connection.execute_wrapper(signal):
                observations.record_staff_fact(
                    receipt_case.graph.actor,
                    subject_id=subject.pk,
                    kind="payment",
                    period=PERIOD,
                    expected_head_id=head.pk,
                )
        except observations.StaleObservationError:
            return "stale"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run_in_thread_as_runtime, subject.tenant_id, lambda: correct("first")
        )
        assert first_read.wait(SIGNAL_TIMEOUT)
        second = pool.submit(
            run_in_thread_as_runtime, subject.tenant_id, lambda: correct("second")
        )
        results = [first.result(timeout=30), second.result(timeout=30)]

    with tenant_context(subject.tenant_id):
        assert sorted(results) == ["stale", "written"]
        assert SourceObservation.objects.filter(supersedes=head).count() == 1


def test_two_actual_calendar_producers_cannot_duplicate_the_same_schedule(
    receipt_case: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenant_context(receipt_case.graph.tenant.pk):
        subject = Obligation.objects.create(
            tenant=receipt_case.graph.tenant,
            client=receipt_case.graph.client,
            obligation_type_id="DAS",
            competence_month=date(2028, 9, 1),
            nominal_due_date=date(2028, 10, 20),
            resolved_due_date=date(2028, 10, 20),
        )
    first_dedupe, rival_progress = Event(), Event()

    def before_insert() -> None:
        if WORKER.get() == "first":
            first_dedupe.set()
            assert rival_progress.wait(SIGNAL_TIMEOUT)
        else:
            rival_progress.set()

    def before_rival_lock(sql: str) -> None:
        if _locking(sql, "obligations_obligation"):
            rival_progress.set()

    monkeypatch.setattr(
        observations,
        "assert_fact",
        _observe(observations.assert_fact, before=before_insert),
    )

    def produce(label: str) -> int:
        WORKER.set(label)
        _runtime_role()
        signal = SQLSignal(before=before_rival_lock if label == "second" else None)
        with connection.execute_wrapper(signal):
            return observations.emit_calendar_assertions([subject])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run_in_thread_as_runtime, subject.tenant_id, lambda: produce("first")
        )
        assert first_dedupe.wait(SIGNAL_TIMEOUT)
        second = pool.submit(
            run_in_thread_as_runtime, subject.tenant_id, lambda: produce("second")
        )
        results = [first.result(timeout=30), second.result(timeout=30)]

    with tenant_context(subject.tenant_id):
        assert sorted(results) == [0, 1]
        assert (
            SourceObservation.objects.filter(
                subject_ref=subject, kind="calendar_scheduled"
            ).count()
            == 1
        )


@pytest.mark.parametrize("materializer_first", [False, True])
def test_review_and_materializer_serialize_in_both_actual_lock_orders(
    receipt_case: PortalSetup, materializer_first: bool
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    order = ReviewMaterializerOrder(materializer_first)

    def decide() -> None:
        _runtime_role()
        with connection.execute_wrapper(
            SQLSignal(order.review_before, order.review_after)
        ):
            _decide(receipt_case, link)

    def settle() -> None:
        _runtime_role()
        with connection.execute_wrapper(
            SQLSignal(order.materializer_before, order.materializer_after)
        ):
            receipt_observations.settle_link(link.pk)

    first_call, second_call = (
        (settle, decide) if materializer_first else (decide, settle)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run_in_thread_as_runtime, receipt_case.graph.tenant.pk, first_call
        )
        assert order.held.wait(SIGNAL_TIMEOUT)
        second = pool.submit(
            run_in_thread_as_runtime, receipt_case.graph.tenant.pk, second_call
        )
        first.result(timeout=30)
        second.result(timeout=30)

    with tenant_context(receipt_case.graph.tenant.pk):
        rows = SourceObservation.objects.filter(value_json__link_id=str(link.pk))
        assert list(rows.values_list("outcome", flat=True)) == ["submitted", "accepted"]


@pytest.mark.parametrize("worker", ["scanner", "extractor"])
@pytest.mark.parametrize("materializer_first", [False, True])
def test_phased_finalizer_and_materializer_cannot_deadlock(
    receipt_case: PortalSetup,
    monkeypatch: pytest.MonkeyPatch,
    worker: str,
    materializer_first: bool,
) -> None:
    intake = submit(receipt_case, "recibo_cliente.pdf")
    if worker == "extractor":
        scan_clean(receipt_case, intake)
    with tenant_context(receipt_case.graph.tenant.pk):
        link = EvidenceLink.objects.get(document_id=intake.document_id)
    intake_held, cycle_held = Event(), Event()

    def after_intake() -> None:
        intake_held.set()
        assert cycle_held.wait(SIGNAL_TIMEOUT)

    module = scanning if worker == "scanner" else extraction
    monkeypatch.setattr(
        module, "_linked_cycles", _observe(module._linked_cycles, before=after_intake)
    )

    def cycle_signal(sql: str) -> None:
        if _locking(sql, "cycles_servicecycle") or _locking(
            sql, "cycles_evidencerequest"
        ):
            cycle_held.set()
            if materializer_first:
                assert intake_held.wait(SIGNAL_TIMEOUT)

    def settle() -> None:
        _runtime_role()
        with connection.execute_wrapper(SQLSignal(after=cycle_signal)):
            receipt_observations.settle_link(link.pk)

    def finalize() -> None:
        if worker == "scanner":
            assert (
                scanning.scan_intake(
                    receipt_case.graph.tenant.pk,
                    intake.pk,
                    scanner=scanning.FakeScanner(),
                    now=timezone.now(),
                )
                is scanning.ScanOutcome.VERIFIED
            )
        else:
            assert (
                extraction.extract_intake(
                    receipt_case.graph.tenant.pk, intake.pk, now=timezone.now()
                )
                is extraction.ExtractionOutcome.CLASSIFIED
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        if materializer_first:
            first = pool.submit(
                run_in_thread_as_runtime, receipt_case.graph.tenant.pk, settle
            )
            assert cycle_held.wait(SIGNAL_TIMEOUT)
            second = pool.submit(_phased_runtime, finalize)
        else:
            first = pool.submit(_phased_runtime, finalize)
            assert intake_held.wait(SIGNAL_TIMEOUT)
            second = pool.submit(
                run_in_thread_as_runtime, receipt_case.graph.tenant.pk, settle
            )
        first.result(timeout=30)
        second.result(timeout=30)

    with tenant_context(receipt_case.graph.tenant.pk):
        assert (
            SourceObservation.objects.filter(
                evidence_link=link, outcome="submitted"
            ).count()
            == 1
        )


@pytest.mark.parametrize("worker", ["scanner", "extractor"])
def test_review_refuses_pending_intake_without_blocking_its_finalizer(
    receipt_case: PortalSetup, monkeypatch: pytest.MonkeyPatch, worker: str
) -> None:
    intake = submit(receipt_case, "recibo_cliente.pdf")
    if worker == "extractor":
        scan_clean(receipt_case, intake)
    with tenant_context(receipt_case.graph.tenant.pk):
        link = EvidenceLink.objects.get(document_id=intake.document_id)
    intake_held, review_finished = Event(), Event()

    def after_intake() -> None:
        intake_held.set()
        assert review_finished.wait(SIGNAL_TIMEOUT)

    module = scanning if worker == "scanner" else extraction
    monkeypatch.setattr(
        module, "_linked_cycles", _observe(module._linked_cycles, before=after_intake)
    )

    def finalize() -> None:
        if worker == "scanner":
            assert (
                scanning.scan_intake(
                    receipt_case.graph.tenant.pk,
                    intake.pk,
                    scanner=scanning.FakeScanner(),
                    now=timezone.now(),
                )
                is scanning.ScanOutcome.VERIFIED
            )
        else:
            assert (
                extraction.extract_intake(
                    receipt_case.graph.tenant.pk, intake.pk, now=timezone.now()
                )
                is extraction.ExtractionOutcome.CLASSIFIED
            )

    def decide() -> None:
        _runtime_role()
        try:
            with pytest.raises(IllegalTransition):
                _decide(receipt_case, link)
        finally:
            review_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        finalizer = pool.submit(_phased_runtime, finalize)
        assert intake_held.wait(SIGNAL_TIMEOUT)
        reviewer = pool.submit(
            run_in_thread_as_runtime, receipt_case.graph.tenant.pk, decide
        )
        reviewer.result(timeout=30)
        finalizer.result(timeout=30)


def test_revocation_committed_before_authority_fence_prevents_staff_fact(
    receipt_case: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = receipt_case.graph
    subject = payment_subject(receipt_case)
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
    revocation_held, writer_progress, revoked = Event(), Event(), Event()

    def revoke() -> None:
        _runtime_role()
        assert (
            Membership.objects.filter(tenant=graph.tenant, user=graph.staff).update(
                is_active=False
            )
            == 1
        )
        revocation_held.set()
        assert writer_progress.wait(SIGNAL_TIMEOUT)

    def revoker() -> None:
        run_in_thread_as_runtime(graph.tenant.pk, revoke)
        revoked.set()

    def before_authority_lock(sql: str) -> None:
        if _locking(sql, "accounts_user") or _locking(sql, "tenants_membership"):
            writer_progress.set()

    def after_capability() -> None:
        writer_progress.set()
        assert revoked.wait(SIGNAL_TIMEOUT)

    monkeypatch.setattr(
        observations,
        "require_actor",
        _observe(require_actor, after=after_capability),
    )

    def write() -> bool:
        _runtime_role()
        with connection.execute_wrapper(SQLSignal(before=before_authority_lock)):
            try:
                observations.record_staff_fact(
                    graph.staff, subject_id=subject.pk, kind="payment", period=PERIOD
                )
            except (PermissionDenied, Http404):
                return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        revocation = pool.submit(revoker)
        assert revocation_held.wait(SIGNAL_TIMEOUT)
        writer = pool.submit(run_in_thread_as_runtime, graph.tenant.pk, write)
        did_write = writer.result(timeout=30)
        revocation.result(timeout=30)

    with tenant_context(graph.tenant.pk):
        assert did_write is False
        assert not SourceObservation.objects.filter(
            subject_ref=subject, kind="payment"
        ).exists()
