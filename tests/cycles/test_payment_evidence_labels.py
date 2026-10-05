"""T17 comprovante receipts: binding, the review→observation path and labels.

A `comprovante` request binds to the obligation it pays for while still a
draft; a portal upload against it produces one pending-review submission
observation (client_submitted — the file is evidence, never a verification);
and the review writes the outcome as a staff_recorded sibling: accepted stays
manual, rejected supersedes to nothing, and the portal badge is honest about
all three. The races below run the review and the materializer against each
other deterministically and prove the chain ends with exactly one outcome.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    RequestState,
)
from apps.cycles.extraction.service import extract_intake
from apps.cycles.models import ServiceCycle
from apps.cycles.receipt_observations import settle_link, settle_pending
from apps.cycles.scanning import FakeScanner, ScanOutcome, scan_intake
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from apps.obligations.observations import lock_subjects
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.document_fixtures import fixture as read_fixture
from tests.portal.request_fixtures import (
    PortalSetup,
    portal_setup,
    upload,
)
from tests.support_threads import run_in_thread_as_runtime

DB = pytest.mark.django_db(transaction=True)


@dataclass
class ReceiptSetup:
    """A portal setup plus the bound receipt request this file works on."""

    base: PortalSetup
    receipt: EvidenceRequest


@pytest.fixture
def setup(settings: SettingsWrapper, tmp_path: Path) -> ReceiptSetup:
    settings.MEDIA_ROOT = tmp_path
    base = portal_setup("t17-receipt")
    with tenant_context(base.graph.tenant.pk):
        generate_das_calendar(base.graph.client, starting_from=PERIOD)
        obligation = Obligation.objects.get(
            client=base.graph.client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
        )
        cycle = ServiceCycle.objects.get(pk=base.cycle.pk)
        receipt = create_request(
            base.graph.actor,
            cycle.pk,
            RequestFields(
                title="Comprovante de pagamento do DAS",
                kind="comprovante",
                obligation_id=obligation.pk,
            ),
            expected_version=cycle.version,
        )
        cycle.refresh_from_db()
        send_requests(base.graph.actor, cycle.pk, expected_version=cycle.version)
        receipt.refresh_from_db()
    return ReceiptSetup(base=base, receipt=receipt)


def _submit_to(setup: ReceiptSetup, name: str = "recibo_cliente.pdf") -> DocumentIntake:
    response = setup.base.portal().post(
        f"/solicitacoes/{setup.receipt.pk}/enviar",
        {"file": upload(name, read_fixture(name))},
    )
    assert response.status_code == 302, response.content
    with tenant_context(setup.base.graph.tenant.pk):
        return (
            DocumentIntake.objects.select_related("document")
            .filter(document__original_filename=name)
            .get()
        )


def _review_ready(
    setup: ReceiptSetup, name: str = "recibo_cliente.pdf"
) -> tuple[DocumentIntake, EvidenceLink]:
    intake = _submit_to(setup, name)
    outcome = scan_intake(
        setup.base.graph.tenant.pk,
        intake.pk,
        scanner=FakeScanner(),
        now=timezone.now(),
    )
    assert outcome is ScanOutcome.VERIFIED
    extract_intake(setup.base.graph.tenant.pk, intake.pk, now=timezone.now())
    with tenant_context(setup.base.graph.tenant.pk):
        return intake, EvidenceLink.objects.get(document_id=intake.document_id)


def _review(  # noqa: PLR0913 - mirrors the review form's own fields
    setup: ReceiptSetup,
    link: EvidenceLink,
    *,
    decision: str,
    version: int,
    payment_relevant: bool,
    client_reason: str = "",
) -> None:
    """POST the real review view — the middleware's role switch is the seam."""
    data: dict[str, str] = {
        "decision": decision,
        "client_reason": client_reason,
        "expected_version": str(version),
    }
    if payment_relevant:
        data["payment_relevant"] = "1"
    response = setup.base.firm().post(f"/documentos/{link.pk}/revisar/", data)
    assert response.status_code == 302, response.content


def _request_version(setup: ReceiptSetup) -> int:
    with tenant_context(setup.base.graph.tenant.pk):
        return EvidenceRequest.objects.get(pk=setup.receipt.pk).version


def _rows(link: EvidenceLink) -> list[SourceObservation]:
    return list(
        SourceObservation.objects.filter(
            kind="payment", value_json__link_id=str(link.pk)
        )
        | SourceObservation.objects.filter(evidence_link_id=link.pk)
    )


def _payments_page(setup: ReceiptSetup) -> str:
    response = setup.base.portal().get("/pagamentos/")
    assert response.status_code == 200
    return response.content.decode()


@DB
def test_comprovante_create_requires_the_bound_obligation(
    setup: ReceiptSetup,
) -> None:
    # Given a receipt request with no obligation to bind to, the service refuses
    with tenant_context(setup.base.graph.tenant.pk), pytest.raises(ValidationError):
        create_request(
            setup.base.graph.actor,
            setup.base.cycle.pk,
            RequestFields(title="Comprovante solto", kind="comprovante"),
            expected_version=ServiceCycle.objects.get(pk=setup.base.cycle.pk).version,
        )


@DB
def test_documento_requests_refuse_an_obligation(setup: ReceiptSetup) -> None:
    # Given a generic request that tries to claim a payment subject anyway
    with tenant_context(setup.base.graph.tenant.pk):
        obligation = Obligation.objects.get(
            client=setup.base.graph.client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
        )
        with pytest.raises(ValidationError):
            create_request(
                setup.base.graph.actor,
                setup.base.cycle.pk,
                RequestFields(
                    title="Documento com vínculo",
                    kind="documento",
                    obligation_id=obligation.pk,
                ),
                expected_version=ServiceCycle.objects.get(
                    pk=setup.base.cycle.pk
                ).version,
            )


@DB
def test_a_portal_submission_materializes_pending_review(
    setup: ReceiptSetup,
) -> None:
    # Given the receipt request sent and the client's upload against it
    _intake, link = _review_ready(setup)

    # When the materializer runs it writes exactly one submission assertion
    with tenant_context(setup.base.graph.tenant.pk):
        submission = settle_link(link.pk)
        assert submission is not None
        assert submission.source_kind == "client_submitted"
        assert submission.verification_status == "pending_review"
        assert submission.outcome == "submitted"
        assert submission.evidence_link_id == link.pk
        assert submission.subject_ref_id == setup.receipt.obligation_id
        assert submission.period == PERIOD

        # And a replay is the same row, never a second root
        again = settle_link(link.pk)
        assert again is not None
        assert again.pk == submission.pk


@DB
def test_acceptance_with_relevance_stays_a_manual_claim(setup: ReceiptSetup) -> None:
    # Given a submitted receipt the reviewer accepts as payment evidence
    _intake, link = _review_ready(setup)
    with tenant_context(setup.base.graph.tenant.pk):
        settle_link(link.pk)
        version = EvidenceRequest.objects.get(pk=setup.receipt.pk).version
    _review(setup, link, decision="aceito", version=version, payment_relevant=True)

    # Then the acceptance is staff_recorded and manual — never verified
    with tenant_context(setup.base.graph.tenant.pk):
        rows = _rows(link)
        assert len(rows) == 2
        accepted = next(row for row in rows if row.outcome == "accepted")
        assert accepted.source_kind == "staff_recorded"
        assert accepted.verification_status == "unknown"

    # And the portal badge still says informed, never confirmed — the HTTP read
    # stays outside tenant_context so SET LOCAL ROLE can never merge upward
    body = _payments_page(setup)
    assert 'data-payment-state="manual"' in body
    assert "Pagamento informado" in body
    assert "confirmado" not in body


@DB
def test_acceptance_without_relevance_writes_no_payment_claim(
    setup: ReceiptSetup,
) -> None:
    # Given a submitted receipt the reviewer accepts but marks not relevant
    _intake, link = _review_ready(setup)
    with tenant_context(setup.base.graph.tenant.pk):
        settle_link(link.pk)
        version = EvidenceRequest.objects.get(pk=setup.receipt.pk).version
    _review(setup, link, decision="aceito", version=version, payment_relevant=False)

    # Then no accepted payment claim exists: the file was accepted as a
    # document, not as proof this obligation was paid
    with tenant_context(setup.base.graph.tenant.pk):
        rows = _rows(link)
        assert [row.outcome for row in rows] == ["submitted"]
    body = _payments_page(setup)
    assert 'data-payment-state="manual"' in body
    assert "aguardando conferência" in body


@DB
def test_rejection_supersedes_the_submission_to_nothing(setup: ReceiptSetup) -> None:
    # Given a submitted receipt the reviewer rejects
    _intake, link = _review_ready(setup)
    with tenant_context(setup.base.graph.tenant.pk):
        submission = settle_link(link.pk)
        version = EvidenceRequest.objects.get(pk=setup.receipt.pk).version
    _review(
        setup,
        link,
        decision="rejeitado",
        version=version,
        payment_relevant=False,
        client_reason="Arquivo ilegível. Envie o comprovante em PDF.",
    )

    # Then the rejection is a superseding unsupported head — the file
    # stays readable in the chain and stops counting as evidence
    with tenant_context(setup.base.graph.tenant.pk):
        rows = _rows(link)
        rejected = next(row for row in rows if row.outcome == "rejected")
        assert submission is not None
        assert rejected.supersedes_id == submission.pk
        assert rejected.source_kind == "staff_recorded"
        assert rejected.verification_status == "unsupported"

    # And the portal falls back to the honest badge
    body = _payments_page(setup)
    assert 'data-payment-state="unknown"' in body
    assert "Pagamento informado" not in body


@DB
def test_a_second_independent_receipt_corroborates_as_a_sibling(
    setup: ReceiptSetup,
) -> None:
    # Given an accepted receipt and a second file submitted for the same claim
    _intake, first = _review_ready(setup)
    with tenant_context(setup.base.graph.tenant.pk):
        settle_link(first.pk)
        version = EvidenceRequest.objects.get(pk=setup.receipt.pk).version
    _review(setup, first, decision="aceito", version=version, payment_relevant=True)
    with tenant_context(setup.base.graph.tenant.pk):
        # The rejection path reopens the request to the client (S6.3); a fresh
        # upload needs an open state again.
        request = EvidenceRequest.objects.get(pk=setup.receipt.pk)
        request.state = RequestState.REJECTED
        request.save(update_fields=["state"])
    _intake2, second = _review_ready(setup, "nfe_emitente_cliente.xml")

    with tenant_context(setup.base.graph.tenant.pk):
        settle_link(second.pk)
        version = EvidenceRequest.objects.get(pk=setup.receipt.pk).version
    _review(setup, second, decision="aceito", version=version, payment_relevant=True)

    # Then the corroborating file is a sibling root, never a replacement
    with tenant_context(setup.base.graph.tenant.pk):
        first_accepted = SourceObservation.objects.get(
            kind="payment",
            value_json__link_id=str(first.pk),
            outcome="accepted",
        )
        second_accepted = SourceObservation.objects.get(
            kind="payment",
            value_json__link_id=str(second.pk),
            outcome="accepted",
        )
        assert first_accepted.supersedes_id is None
        assert second_accepted.supersedes_id is None
    body = _payments_page(setup)
    assert 'data-payment-state="manual"' in body


@DB
def test_sweep_materializes_a_missed_link_and_is_idempotent(
    setup: ReceiptSetup,
) -> None:
    # Given a submission whose on-commit enqueue never ran
    _intake, link = _review_ready(setup)
    with tenant_context(setup.base.graph.tenant.pk):
        assert SourceObservation.objects.filter(evidence_link_id=link.pk).count() == 0

        # Then the sweep writes it once and a second pass writes nothing
        assert settle_pending(setup.base.graph.tenant) == 1
        assert settle_pending(setup.base.graph.tenant) == 0
        assert SourceObservation.objects.filter(evidence_link_id=link.pk).count() == 1


@DB
def test_race_review_versus_materializer_one_submission_one_outcome(
    setup: ReceiptSetup,
) -> None:
    """Review and settle race; the chain ends single-headed with one outcome."""
    # Given a submitted receipt not yet materialized
    _intake, link = _review_ready(setup)
    gate = Event()
    with tenant_context(setup.base.graph.tenant.pk):
        version = EvidenceRequest.objects.get(pk=setup.receipt.pk).version

    def review() -> None:
        gate.wait(timeout=10)
        # The HTTP surface: the middleware's role switch grants audit access a
        # bare service call in a thread cannot get.
        data = {
            "decision": "aceito",
            "client_reason": "",
            "expected_version": str(version),
            "payment_relevant": "1",
        }
        assert (
            setup.base.firm().post(f"/documentos/{link.pk}/revisar/", data).status_code
            == 302
        )

    def settle() -> None:
        gate.wait(timeout=10)
        settle_link(link.pk)

    # When both writers race, the locks serialize them to one consistent chain
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(run_in_thread_as_runtime, setup.base.graph.tenant.pk, review)
        two = pool.submit(run_in_thread_as_runtime, setup.base.graph.tenant.pk, settle)
        gate.set()
        one.result(timeout=30)
        two.result(timeout=30)

    with tenant_context(setup.base.graph.tenant.pk):
        assert SourceObservation.objects.filter(evidence_link_id=link.pk).count() == 1
        assert (
            SourceObservation.objects.filter(
                kind="payment",
                value_json__link_id=str(link.pk),
                outcome="accepted",
            ).count()
            == 1
        )


@DB
def test_race_blocked_on_subject_lock_converges_after_commit(
    setup: ReceiptSetup,
) -> None:
    """Writers queued behind the subject lock converge — never duplicate."""
    # Given a submission and a thread pinning the subject lock open
    _intake, link = _review_ready(setup)
    held = Event()
    release = Event()
    with tenant_context(setup.base.graph.tenant.pk):
        version = EvidenceRequest.objects.get(pk=setup.receipt.pk).version
        obligation_id = setup.receipt.obligation_id
        assert obligation_id is not None

    def holder() -> None:
        with transaction.atomic():
            list(lock_subjects([obligation_id]))
            held.set()
            assert release.wait(timeout=10)

    def review() -> None:
        data = {
            "decision": "aceito",
            "client_reason": "",
            "expected_version": str(version),
            "payment_relevant": "1",
        }
        assert (
            setup.base.firm().post(f"/documentos/{link.pk}/revisar/", data).status_code
            == 302
        )

    keeper = Thread(
        target=lambda: run_in_thread_as_runtime(setup.base.graph.tenant.pk, holder),
        daemon=True,
    )
    reviewer = Thread(
        target=lambda: run_in_thread_as_runtime(setup.base.graph.tenant.pk, review),
        daemon=True,
    )
    keeper.start()
    assert held.wait(timeout=10)
    reviewer.start()

    # When the lock releases, the review completes and the settle afterwards
    # finds both rows already written — replay, never a twin
    release.set()
    reviewer.join(timeout=15)
    settled = run_in_thread_as_runtime(
        setup.base.graph.tenant.pk, lambda: settle_link(link.pk)
    )
    keeper.join(timeout=10)

    with tenant_context(setup.base.graph.tenant.pk):
        assert settled is not None
        assert SourceObservation.objects.filter(evidence_link_id=link.pk).count() == 1
        assert (
            SourceObservation.objects.filter(
                kind="payment",
                value_json__link_id=str(link.pk),
                outcome="accepted",
            ).count()
            == 1
        )


@DB
def test_the_subject_lock_nowait_contract_holds(setup: ReceiptSetup) -> None:
    """The row lock every observation writer takes is visible to NOWAIT."""
    # Given a transaction on one connection holding the obligation row — a
    # second writer on another connection is what NOWAIT must refuse
    held = Event()
    done = Event()
    outcome: dict[str, object] = {}

    with tenant_context(setup.base.graph.tenant.pk):
        obligation_id = Obligation.objects.get(
            client=setup.base.graph.client,
            obligation_type_id="DAS",
            competence_month=PERIOD,
        ).pk

    def holder() -> None:
        with transaction.atomic():
            list(lock_subjects([obligation_id]))
            held.set()
            assert done.wait(timeout=10)

    def probe() -> None:
        held.wait(timeout=10)
        try:
            with transaction.atomic():
                list(
                    Obligation.objects.filter(pk=obligation_id).select_for_update(
                        nowait=True
                    )
                )
            outcome["result"] = "acquired"
        except OperationalError:
            outcome["result"] = "locked"

    keeper = Thread(
        target=lambda: run_in_thread_as_runtime(setup.base.graph.tenant.pk, holder),
        daemon=True,
    )
    keeper.start()
    assert held.wait(timeout=10)
    run_in_thread_as_runtime(setup.base.graph.tenant.pk, probe)
    done.set()
    keeper.join(timeout=10)

    # Then the probe on its own connection saw the lock the holder kept
    assert outcome.get("result") == "locked"
