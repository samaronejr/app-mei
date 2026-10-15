"""B3/B4: qualified receipts, immutable successors and exact-effect invalidation."""

from dataclasses import replace
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.clients.maintenance import EngagementData, save_engagement
from apps.clients.models import ClientAssignment
from apps.core.tenancy import tenant_context
from apps.cycles import receipt_observations
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    RequestInternal,
    Review,
    ScanVerdict,
)
from apps.cycles.models import ActionExecution, CycleEvent, ProposalState
from apps.cycles.receipt_observations import settle_link, settle_pending
from apps.cycles.services import approvals
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.obligations.coverage import coverage_for
from apps.obligations.models import SourceObservation
from apps.obligations.observations import retract_staff_fact
from apps.tenants.models import Tenant
from tests.cycles import observation_fixtures
from tests.cycles.cycle_fixtures import PERIOD
from tests.cycles.document_fixtures import reviewed, submit
from tests.cycles.observation_fixtures import (
    coverage_item,
    payment_subject,
    review_post,
)
from tests.portal.request_fixtures import PortalSetup

receipt_case = observation_fixtures.receipt_case
pytestmark = pytest.mark.django_db(transaction=True)


class ReceiptInterruptionError(RuntimeError):
    """A forced interruption after the real insertion but before commit."""


@pytest.mark.parametrize("rebind", [False, True], ids=["create", "draft-rebind"])
@pytest.mark.parametrize(
    ("start_offset", "end_offset", "allowed"),
    [(0, None, True), (1, None, False), (-1, 0, False), (-1, 1, True)],
    ids=["starts-at", "starts-after", "ends-at", "ends-after"],
)
def test_receipt_binding_requires_the_engagement_window(
    receipt_case: PortalSetup,
    start_offset: int,
    end_offset: int | None,
    allowed: bool,
    rebind: bool,
) -> None:
    graph = receipt_case.graph
    subject = payment_subject(receipt_case)
    payload = {
        "action": "create",
        "title": "Comprovante da competência",
        "kind": "comprovante",
        "obligation_id": str(subject.pk),
        "instructions": "Conferir o comprovante.",
    }
    # Given a real engagement edit after the cycle (and any draft) already exists.
    with tenant_context(graph.tenant.pk):
        if rebind:
            draft = create_request(
                graph.actor,
                receipt_case.cycle.pk,
                RequestFields("Documento em rascunho", instructions="Preservar."),
                expected_version=receipt_case.cycle.version,
            )
            payload.update(
                action="update",
                request_id=str(draft.pk),
                expected_version=str(draft.version),
            )
        receipt_case.cycle.refresh_from_db()
        if not rebind:
            payload["expected_version"] = str(receipt_case.cycle.version)
        save_engagement(
            actor=graph.actor,
            tenant_id=graph.tenant.pk,
            client_id=graph.client.pk,
            data=EngagementData(
                service_kind=graph.engagement.service_kind,
                starts_on=PERIOD + timedelta(days=start_offset),
                ends_on=None
                if end_offset is None
                else PERIOD + timedelta(days=end_offset),
                responsible_id=graph.actor.pk,
            ),
            engagement_id=graph.engagement.pk,
            expected_version=graph.engagement.version,
        )
        requests = EvidenceRequest.objects.filter(cycle=receipt_case.cycle).order_by(
            "pk"
        )
        internals = RequestInternal.objects.filter(
            request__cycle=receipt_case.cycle
        ).order_by("pk")
        events = CycleEvent.objects.filter(cycle=receipt_case.cycle).order_by("pk")
        before = (
            list(requests.values()),
            list(internals.values()),
            list(events.values()),
            receipt_case.cycle.version,
        )

    # When the real HTTP command binds the actual obligation for that competence.
    response = receipt_case.firm().post(
        f"/ciclos/{receipt_case.cycle.pk}/solicitacoes/", payload
    )

    # Then refusal is atomic, while both applicable edges retain historical reads.
    with tenant_context(graph.tenant.pk):
        receipt_case.cycle.refresh_from_db()
        coverage = coverage_for(graph.actor, graph.client, PERIOD)
        if allowed:
            assert response.status_code == 302
            assert len(coverage) == 1
            assert requests.count() == len(before[0]) + (0 if rebind else 1)
            assert internals.count() == len(before[1]) + (0 if rebind else 1)
            assert events.count() == len(before[2]) + 1
            bound = requests.get(title=payload["title"])
            assert (bound.kind, bound.obligation_id) == ("comprovante", subject.pk)
            assert receipt_case.cycle.version == before[3] + (0 if rebind else 1)
        else:
            assert response.status_code == 422
            assert coverage == []
            assert (
                list(requests.values()),
                list(internals.values()),
                list(events.values()),
                receipt_case.cycle.version,
            ) == before


def test_clean_receipt_has_an_explicit_unselected_payment_control(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")

    response = receipt_case.firm().get(f"/documentos/{link.pk}/revisar/")

    assert response.status_code == 200
    html = response.content.decode()
    assert html.count('name="payment_relevant"') == 1
    control = html.split('name="payment_relevant"', 1)[1].split(">", 1)[0]
    assert "checked" not in control
    assert review_post(receipt_case, link).status_code == 302
    assert coverage_item(receipt_case).state == "manual"


@pytest.mark.parametrize("materialize_first", [True, False], ids=["job", "review"])
def test_acceptance_supersedes_its_exact_submission_with_review_provenance(
    receipt_case: PortalSetup, materialize_first: bool
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    original = None
    with tenant_context(receipt_case.graph.tenant.pk):
        if materialize_first:
            submission = settle_link(link.pk)
            assert submission is not None
            original = SourceObservation.objects.filter(pk=submission.pk).values().get()

    assert review_post(receipt_case, link).status_code == 302

    with tenant_context(receipt_case.graph.tenant.pk):
        settle_link(link.pk)
        rows = SourceObservation.objects.filter(value_json__link_id=str(link.pk))
        submission = rows.get(outcome="submitted")
        accepted = rows.get(outcome="accepted")
        review = Review.objects.get(link=link)
        assert rows.count() == 2
        assert accepted.supersedes_id == submission.pk
        assert accepted.evidence_link_id == link.pk
        assert accepted.value_json["review_id"] == str(review.pk)
        assert accepted.value_json["submission_id"] == str(submission.pk)
        assert accepted.value_json["sha256"] == review.document_sha256
        assert accepted.observed_at == review.reviewed_at
        assert accepted.recorded_by_id == review.reviewer_id
        assert accepted.source_kind == "staff_recorded"
        assert accepted.verification_status == "unknown"
        if original is not None:
            assert (
                SourceObservation.objects.filter(pk=submission.pk).values().get()
                == original
            )
    assert coverage_item(receipt_case).head_id == accepted.pk


def test_retraction_never_resurrects_the_accepted_receipts_submission(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    assert review_post(receipt_case, link).status_code == 302
    subject = payment_subject(receipt_case)
    with tenant_context(subject.tenant_id):
        accepted = SourceObservation.objects.get(
            value_json__link_id=str(link.pk), outcome="accepted"
        )
        retract_staff_fact(
            receipt_case.graph.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
            expected_head_id=accepted.pk,
        )
        settle_link(link.pk)
        assert settle_pending(receipt_case.graph.tenant) == 0

    response = receipt_case.portal().get("/pagamentos/")

    assert coverage_item(receipt_case).state == "unknown"
    assert 'data-payment-state="manual"' not in response.content.decode()
    assert "badge-ok" not in response.content.decode()


def test_unscanned_receipt_cannot_produce_an_accepted_payment(
    receipt_case: PortalSetup,
) -> None:
    intake = submit(receipt_case, "recibo_cliente.pdf")
    with tenant_context(receipt_case.graph.tenant.pk):
        DocumentIntake.objects.filter(pk=intake.pk).update(
            state=IntakeState.SCAN_UNAVAILABLE, verdict=ScanVerdict.UNAVAILABLE
        )
        link = EvidenceLink.objects.get(document_id=intake.document_id)

    response = review_post(receipt_case, link)

    assert response.status_code == 302
    with tenant_context(receipt_case.graph.tenant.pk):
        assert Review.objects.get(link=link).decision == "aceito"
        assert not SourceObservation.objects.filter(
            value_json__link_id=str(link.pk), outcome="accepted"
        ).exists()


def test_divergent_third_party_document_cannot_become_payment_evidence(
    receipt_case: PortalSetup,
) -> None:
    intake, link = reviewed(receipt_case, "nfe_outra_empresa.xml")
    assert intake.state == IntakeState.DIVERGENT

    response = review_post(
        receipt_case,
        link,
        third_party="1",
        override_reason="Documento do fornecedor solicitado pela equipe.",
    )

    assert response.status_code == 302
    with tenant_context(receipt_case.graph.tenant.pk):
        assert Review.objects.get(link=link).third_party is True
        settle_link(link.pk)
        assert not SourceObservation.objects.filter(
            value_json__link_id=str(link.pk), outcome="accepted"
        ).exists()


def test_clean_relink_of_a_third_party_document_does_not_launder_divergence(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "nfe_outra_empresa.xml")
    assert (
        review_post(
            receipt_case,
            link,
            third_party="1",
            override_reason="Documento do fornecedor.",
        ).status_code
        == 302
    )
    with tenant_context(receipt_case.graph.tenant.pk):
        receipt_case.cycle.refresh_from_db()
        request = create_request(
            receipt_case.graph.actor,
            receipt_case.cycle.pk,
            RequestFields(
                "Outro comprovante",
                kind="comprovante",
                obligation_id=receipt_case.request.obligation_id,
            ),
            expected_version=receipt_case.cycle.version,
        )
        receipt_case.cycle.refresh_from_db()
        send_requests(
            receipt_case.graph.actor,
            receipt_case.cycle.pk,
            expected_version=receipt_case.cycle.version,
        )
    second = replace(receipt_case, request=request)
    submit(second, "nfe_outra_empresa.xml")
    with tenant_context(second.graph.tenant.pk):
        relink = EvidenceLink.objects.get(request=request)

    assert review_post(second, relink).status_code == 302

    with tenant_context(second.graph.tenant.pk):
        assert relink.document_id == link.document_id
        assert not SourceObservation.objects.filter(
            value_json__link_id=str(relink.pk), outcome="accepted"
        ).exists()


@pytest.mark.parametrize("reject_second", [False, True], ids=["pending", "rejected"])
def test_independent_receipt_does_not_erase_accepted_knowledge(
    receipt_case: PortalSetup, reject_second: bool
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    assert review_post(receipt_case, link).status_code == 302
    with tenant_context(receipt_case.graph.tenant.pk):
        accepted = SourceObservation.objects.get(
            value_json__link_id=str(link.pk), outcome="accepted"
        )
        receipt_case.cycle.refresh_from_db()
        request = create_request(
            receipt_case.graph.actor,
            receipt_case.cycle.pk,
            RequestFields(
                "Outro comprovante",
                kind="comprovante",
                obligation_id=receipt_case.request.obligation_id,
            ),
            expected_version=receipt_case.cycle.version,
        )
        receipt_case.cycle.refresh_from_db()
        send_requests(
            receipt_case.graph.actor,
            receipt_case.cycle.pk,
            expected_version=receipt_case.cycle.version,
        )
    second = replace(receipt_case, request=request)
    submit(second, "recibo_cliente.pdf")
    with tenant_context(second.graph.tenant.pk):
        relink = EvidenceLink.objects.get(request=request)
        assert relink.document_id == link.document_id
        assert settle_link(relink.pk) is not None
    if reject_second:
        assert (
            review_post(
                second,
                relink,
                decision="rejeitado",
                client_reason="Não sustenta a alegação.",
            ).status_code
            == 302
        )

    result = coverage_item(receipt_case)

    assert result.state == "manual"
    assert result.head_id == accepted.pk
    assert result.last_success == accepted.observed_at
    assert 'data-payment-state="manual"' in (
        receipt_case.portal().get("/pagamentos/").content.decode()
    )


def test_forged_portal_link_decision_never_stands_in_for_a_review(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    with tenant_context(receipt_case.graph.tenant.pk):
        EvidenceLink.objects.filter(pk=link.pk).update(review_decision="aceito")

        result = settle_link(link.pk)

        assert result is not None
        assert result.outcome == "submitted"
        assert not Review.objects.filter(link=link).exists()
        assert not SourceObservation.objects.filter(
            value_json__link_id=str(link.pk), outcome="accepted"
        ).exists()


def test_receipt_acceptance_invalidates_the_approved_exact_submission(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    graph = receipt_case.graph
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
        submission = settle_link(link.pk)
        assert submission is not None
        receipt_case.cycle.refresh_from_db()
        proposal = approvals.propose(
            graph.staff,
            receipt_case.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="t17-selected-submission",
                    recipient_ids=(receipt_case.contact.pk,),
                    observation_ids=(submission.pk,),
                    subject="Comprovante recebido",
                    body="Confira o comprovante selecionado.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=receipt_case.cycle.version,
        )
        approvals.approve(graph.actor, proposal.pk, expected_version=1)
    assert review_post(receipt_case, link).status_code == 302

    with tenant_context(graph.tenant.pk):
        result = execute(graph.staff, proposal.pk, expected_version=2)
        proposal.refresh_from_db()

        assert result.executed is False
        assert proposal.state == ProposalState.INVALIDATED
        assert not ActionExecution.objects.filter(proposal=proposal).exists()


def test_review_cannot_fork_an_explicitly_retracted_submission(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    subject = payment_subject(receipt_case)
    with tenant_context(subject.tenant_id):
        submission = settle_link(link.pk)
        assert submission is not None
        retract_staff_fact(
            receipt_case.graph.actor,
            subject_id=subject.pk,
            kind="payment",
            period=PERIOD,
            expected_head_id=submission.pk,
        )
        before = list(
            SourceObservation.objects.filter(subject_ref=subject).values_list(
                "pk", "supersedes_id"
            )
        )

    response = review_post(receipt_case, link)

    assert response.status_code == 409
    with tenant_context(subject.tenant_id):
        assert not Review.objects.filter(link=link).exists()
        assert (
            list(
                SourceObservation.objects.filter(subject_ref=subject).values_list(
                    "pk", "supersedes_id"
                )
            )
            == before
        )


@pytest.mark.parametrize("state", [IntakeState.CLASSIFIED, IntakeState.LINKED])
def test_ready_state_without_a_clean_verdict_is_not_qualified_payment(
    receipt_case: PortalSetup, state: str
) -> None:
    intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    with tenant_context(receipt_case.graph.tenant.pk):
        DocumentIntake.objects.filter(pk=intake.pk).update(
            state=state, verdict=ScanVerdict.UNAVAILABLE
        )

    assert review_post(receipt_case, link).status_code == 302

    with tenant_context(receipt_case.graph.tenant.pk):
        assert not SourceObservation.objects.filter(
            value_json__link_id=str(link.pk), outcome="accepted"
        ).exists()


def test_interrupted_materialization_resumes_once_with_the_original_clock(
    receipt_case: PortalSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    original = receipt_observations._write_submission

    def interrupted(
        *, request: EvidenceRequest, link: EvidenceLink
    ) -> SourceObservation:
        original(request=request, link=link)
        raise ReceiptInterruptionError

    with monkeypatch.context() as context:
        context.setattr(receipt_observations, "_write_submission", interrupted)
        for _attempt in range(2):
            with (
                tenant_context(receipt_case.graph.tenant.pk),
                pytest.raises(ReceiptInterruptionError),
            ):
                settle_link(link.pk)
            with tenant_context(receipt_case.graph.tenant.pk):
                assert EvidenceLink.objects.filter(pk=link.pk).exists()
                assert not SourceObservation.objects.filter(evidence_link=link).exists()

    with tenant_context(receipt_case.graph.tenant.pk):
        result = settle_link(link.pk)
        assert result is not None
        assert result.observed_at == link.created_at
        replay = settle_link(link.pk)
        assert replay is not None
        assert replay.pk == result.pk
        assert (
            SourceObservation.objects.filter(
                evidence_link=link, outcome="submitted"
            ).count()
            == 1
        )


def test_late_materializer_cannot_write_after_rollout_is_disabled(
    receipt_case: PortalSetup,
) -> None:
    _intake, link = reviewed(receipt_case, "recibo_cliente.pdf")
    with tenant_context(receipt_case.graph.tenant.pk):
        Tenant.objects.filter(pk=receipt_case.graph.tenant.pk).update(features=[])
        before = SourceObservation.objects.count()

        result = settle_link(link.pk)

        assert result is None
        assert SourceObservation.objects.count() == before
