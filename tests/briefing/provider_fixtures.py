"""Real stored provider facts; only the proposal uses its exact-manifest producer."""

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

from django.utils import timezone

from apps.clients.models import (
    ClientCompany,
    ClientContact,
    ClientStatus,
    Engagement,
    TaxProfile,
    TaxRegime,
)
from apps.clients.models.engagement import ServiceKind
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.evidence import LinkOrigin, ScanVerdict
from apps.cycles.models import (
    ActionProposal,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ExceptionInternal,
    ExceptionState,
    IntakeState,
    RequestInternal,
    RequestState,
    ServiceCycle,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import REQUEST_OVERDUE
from apps.cycles.services.approvals import propose
from apps.cycles.services.cycle import open_cycle
from apps.delivery.models import MessageState, OutboundMessage
from apps.obligations.models import (
    Document,
    ObservationVerificationStatus,
)
from apps.obligations.models.observations import ObservationOutcome
from apps.obligations.observations import assert_fact
from tests.briefing.fixtures import NOW, due_obligation
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup
from tests.cycles.document_fixtures import fixture
from tests.ui.factories import make_cnpj


@dataclass(frozen=True, slots=True)
class ProviderGraph:
    setup: CycleSetup
    cycle: ServiceCycle
    subjects: dict[str, UUID]
    proposal: ActionProposal
    message: OutboundMessage
    intake: DocumentIntake
    link: EvidenceLink
    request: EvidenceRequest
    problem: CycleException


def second_client(setup: CycleSetup) -> CycleSetup:
    with tenant_context(setup.tenant.pk):
        client = ClientCompany.objects.create(
            tenant=setup.tenant,
            cnpj=make_cnpj("12ABC3450002"),
            legal_name="Outra empresa da mesma carteira",
            status=ClientStatus.ACTIVE,
        )
        engagement = Engagement.objects.create(
            tenant=setup.tenant,
            client=client,
            service_kind=ServiceKind.MEI_MENSAL,
            starts_on=PERIOD,
            responsible=setup.actor,
            provenance="staff_entry",
        )
        TaxProfile.objects.create(
            tenant=setup.tenant,
            client=client,
            regime=TaxRegime.MEI,
            valid_from=PERIOD,
            provenance="staff_entry",
        )
    return CycleSetup(
        setup.tenant,
        setup.actor,
        setup.staff,
        client,
        engagement,
        setup.definition,
    )


def provider_graph(setup: CycleSetup) -> ProviderGraph:
    obligation = due_obligation(setup)
    with tenant_context(setup.tenant.pk):
        ref = open_cycle(
            setup.actor,
            setup.engagement.pk,
            PERIOD,
            setup.definition.pk,
            idempotency_key=f"briefing-{setup.client.pk}",
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        request = EvidenceRequest.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=cycle,
            title="Notas do mês",
            due_on=NOW.date() - timedelta(days=1),
            state=RequestState.REQUESTED,
            created_by=setup.actor,
            requested_at=NOW - timedelta(days=2),
        )
        RequestInternal.objects.create(
            tenant=setup.tenant, client=setup.client, request=request
        )
        problem = CycleException.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=cycle,
            subject_ref=request.pk,
            reason_code=REQUEST_OVERDUE,
            state=ExceptionState.OPEN,
            owner=setup.actor,
            due_on=NOW.date(),
            client_visible=True,
            client_message="Precisamos das notas para revisar o mês.",
        )
        ExceptionInternal.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            exception=problem,
            next_action="Conferir a solicitação e orientar o cliente",
            evidence_refs=[str(request.pk)],
        )
        contact = ClientContact.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            name="Contato sintético",
            email="synthetic@example.test",
        )
        message = OutboundMessage.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            cycle=cycle,
            contact=contact,
            created_by=setup.actor,
            idempotency_key=f"briefing-{setup.client.pk}",
            business_intent_key=f"briefing-{setup.client.pk}",
            content_digest="b" * 64,
            state=MessageState.UNKNOWN,
            subject="Solicitação do mês",
            body_ref="synthetic notice",
            portal_url="https://portal.example.test/",
            updated_at=NOW,
        )
        payload = fixture("recibo_outra_empresa.pdf")
        document = Document.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            sha256=sha256(payload).hexdigest(),
            original_filename="nota-de-outra-empresa.pdf",
            content_type="application/pdf",
            byte_size=len(payload),
            uploaded_by=setup.actor,
        )
        link = EvidenceLink.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            request=request,
            document=document,
            origin=LinkOrigin.STAFF,
            submitted_by=setup.actor,
        )
        intake = DocumentIntake.objects.create(
            tenant=setup.tenant,
            client=setup.client,
            document=document,
            state=IntakeState.DIVERGENT,
            verdict=ScanVerdict.CLEAN,
            scanned_at=NOW,
        )
        assert_fact(
            tenant_id=setup.tenant.pk,
            client_id=setup.client.pk,
            subject_id=obligation.pk,
            kind="payment",
            period=PERIOD,
            source_kind="official_api",
            observed_at=NOW - timedelta(days=9),
            value={},
            verification_status=ObservationVerificationStatus.STALE,
            outcome=ObservationOutcome.RECORDED,
        )
        cycle.refresh_from_db()
        proposal = propose(
            setup.actor,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="notice",
                    recipient_ids=(contact.pk,),
                    subject="Revisão do mês",
                    body="Consulte as solicitações no portal.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
    return ProviderGraph(
        setup,
        cycle,
        {
            "obligation": obligation.pk,
            "request": request.pk,
            "exception": problem.pk,
            "approval": proposal.pk,
            "delivery": message.pk,
            "document": intake.pk,
            "coverage": setup.engagement.pk,
        },
        proposal,
        message,
        intake,
        link,
        request,
        problem,
    )
