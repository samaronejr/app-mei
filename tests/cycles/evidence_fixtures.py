"""Scenario A uses the real import, portal, review, approval and SMTP boundaries."""

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from django.test import override_settings
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.clients.importing import commit_import, plan_import
from apps.clients.maintenance import (
    EngagementData,
    save_engagement,
    set_tax_profile,
    update_client,
)
from apps.clients.models import ClientAssignment, ClientCompany, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.executors import execute
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.export_records import EvidenceIndex
from apps.cycles.models import (
    ClientPublication,
    CompletionGeneration,
    CycleState,
    CycleTask,
    EvidenceLink,
    ExceptionState,
    RequestState,
    ReviewDecision,
    ServiceCycle,
    TaskState,
)
from apps.cycles.openers import REQUEST_OVERDUE
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.cycle import open_cycle, transition_cycle, transition_task
from apps.cycles.services.exceptions import open_exception, transition_exception
from apps.cycles.services.requests import (
    RequestFields,
    client_status,
    create_request,
    send_requests,
)
from apps.cycles.services.review import record_review
from apps.delivery.tasks import dispatch_tenant
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Obligation, SourceObservation
from apps.tenants.models import Membership, TenantRole
from tests.clients.test_import_wizard import csv_upload
from tests.cycles.completion_fixtures import CompletionSetup, close_cycle
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.cycles.document_fixtures import fixture, reviewed
from tests.delivery.smtp import FakeSMTP
from tests.portal.request_fixtures import PortalSetup
from tests.support import enrol_totp
from tests.ui.factories import add_member


@dataclass(frozen=True, slots=True)
class EvidenceScenario:
    setup: PortalSetup
    index: EvidenceIndex
    cycle_states: tuple[str, ...]
    request_versions: tuple[int, ...]


def imported_setup(slug: str) -> PortalSetup:
    base = cycle_setup(slug)
    enrol_totp(base.actor)
    enrol_totp(base.staff)
    with tenant_context(base.tenant.pk):
        plan = plan_import(
            base.actor,
            csv_upload([("11444777000161", "Cliente importado cenário A", "SP")]),
            None,
        )
        commit_import(base.actor, plan.batch_id, expected_version=plan.version)
        client = ClientCompany.objects.get(import_batch_id=plan.batch_id)
        client = update_client(
            actor=base.actor,
            tenant_id=base.tenant.pk,
            client_id=client.pk,
            expected_version=client.version,
            changes={"status": "active"},
        )
        set_tax_profile(
            actor=base.actor,
            tenant_id=base.tenant.pk,
            client_id=client.pk,
            regime="mei",
            valid_from=PERIOD,
            expected_revision=client.tax_profile_revision,
            note="Competência inicial conferida no cadastro importado.",
        )
        engagement = save_engagement(
            actor=base.actor,
            tenant_id=base.tenant.pk,
            client_id=client.pk,
            data=EngagementData("mei_mensal", PERIOD, responsible_id=base.actor.pk),
        )
        graph = replace(base, client=client, engagement=engagement)
        ClientAssignment.objects.create(
            tenant=base.tenant, client=client, user=base.staff
        )
        ref = open_cycle(
            base.actor, engagement.pk, PERIOD, base.definition.pk, idempotency_key=slug
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        request = create_request(
            base.actor,
            cycle.pk,
            RequestFields(
                "Nota do período",
                client_note="Envie o XML da nota.",
                due_on=timezone.localdate() - timedelta(days=1),
                instructions="PRIVATE-SCENARIO-A-INSTRUCTIONS",
            ),
            expected_version=cycle.version,
        )
        other_ref = open_cycle(
            base.actor,
            base.engagement.pk,
            PERIOD,
            base.definition.pk,
            idempotency_key=f"{slug}-control",
        )
        other_request = create_request(
            base.actor,
            other_ref.id,
            RequestFields("OTHER-CLIENT-CANARY"),
            expected_version=other_ref.version,
        )
    portal_user = add_member(
        base.tenant, f"client@{slug}.test", TenantRole.CLIENT_OWNER, client=client
    )
    with tenant_context(base.tenant.pk):
        contact = ClientContact.objects.create(
            tenant=base.tenant,
            client=client,
            name="Responsável pelo cliente",
            email=portal_user.email,
            membership=Membership.objects.get(user=portal_user, tenant=base.tenant),
            channel_email_allowed=True,
        )
    return PortalSetup(
        graph, cycle, request, base.client, other_request, portal_user, contact
    )


def advance(setup: PortalSetup, target: str) -> str:
    setup.cycle.refresh_from_db()
    result = transition_cycle(
        setup.graph.actor,
        setup.cycle.pk,
        target,
        expected_version=setup.cycle.version,
    )
    return result.state


def accept_tasks(setup: PortalSetup) -> None:
    for _ in range(CycleTask.objects.filter(cycle=setup.cycle).count()):
        task = (
            CycleTask.objects.filter(cycle=setup.cycle, state=TaskState.READY)
            .order_by("step_key")
            .first()
        )
        assert task is not None
        for target in [TaskState.IN_PROGRESS, TaskState.SUBMITTED, TaskState.ACCEPTED]:
            task = transition_task(
                setup.graph.actor, task.pk, target, expected_version=task.version
            )


def scenario_a(
    settings: SettingsWrapper,
    tmp_path: Path,
    slug: str,
) -> EvidenceScenario:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]
    settings.MEDIA_ROOT = tmp_path
    settings.DELIVERY_PAUSED = False
    setup = imported_setup(slug)
    return complete_evidence_scenario(setup)


def complete_evidence_scenario(setup: PortalSetup) -> EvidenceScenario:
    with (
        FakeSMTP(mode="accept") as peer,
        override_settings(
            EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",
            EMAIL_PORT=peer.port,
            EMAIL_HOST_USER="",
            EMAIL_HOST_PASSWORD="",
            EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,
            EMAIL_TIMEOUT=3,
        ),
    ):
        link, states, versions = _collect_evidence(setup, peer)
        return _publish_and_complete(setup, link, states, versions, peer)


def _collect_evidence(
    setup: PortalSetup,
    peer: FakeSMTP,
) -> tuple[EvidenceLink, list[str], list[int]]:
    versions = [setup.request.version]
    states: list[str] = [CycleState.PLANNED]
    with tenant_context(setup.graph.tenant.pk):
        states.append(advance(setup, CycleState.ACTIVE))
        setup.cycle.refresh_from_db()
        send_requests(
            setup.graph.actor, setup.cycle.pk, expected_version=setup.cycle.version
        )
        setup.request.refresh_from_db()
        assert setup.request.state == RequestState.REQUESTED
        versions.append(setup.request.version)
        states.append(advance(setup, CycleState.WAITING))
        setup.cycle.refresh_from_db()
        problem = open_exception(
            REQUEST_OVERDUE,
            setup.request.pk,
            actor=setup.graph.actor,
            cycle_id=setup.cycle.pk,
            expected_version=setup.cycle.version,
            owner=setup.graph.staff,
            review_on=timezone.localdate(),
            evidence_refs=(setup.request.pk,),
        )
        assert problem.state == ExceptionState.OPEN
        problem = transition_exception(
            setup.graph.actor,
            problem.pk,
            ExceptionState.HANDLING,
            expected_version=problem.version,
        )
    assert dispatch_tenant(str(setup.graph.tenant.pk)) == 1
    assert len(peer.received) == 1
    payload = fixture("nfe_emitente_cliente.xml").replace(
        b"11222333000181", setup.graph.client.cnpj.encode()
    )
    intake, link = reviewed(setup, f"scenario-{setup.graph.client.pk}.xml", payload)
    with tenant_context(setup.graph.tenant.pk):
        assert intake.document.client_id == setup.graph.client.pk
        assert (
            client_status(setup.request, submitted=True, today=timezone.localdate())
            == "recebido"
        )
        states.extend(
            [advance(setup, CycleState.ACTIVE), advance(setup, CycleState.REVIEW)]
        )
        review = record_review(
            setup.graph.actor,
            link.pk,
            decision=ReviewDecision.ACCEPTED,
            client_reason="",
            corrections=[],
            expected_version=setup.request.version,
        )
        versions.append(review.request_version)
        generate_das_calendar(
            setup.graph.client, starting_from=setup.cycle.period_start
        )
        subject = Obligation.objects.select_for_update(no_key=True).get(
            client=setup.graph.client, competence_month=setup.cycle.period_start
        )
        SourceObservation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            subject_ref=subject,
            period=setup.cycle.period_start,
            kind="guide_issued",
            source_kind="staff_recorded",
            verification_status="unknown",
            observed_at=timezone.now(),
            source_ref=f"SCOPED-SOURCE-{setup.graph.client.pk}",
        )
        problem = transition_exception(
            setup.graph.actor,
            problem.pk,
            ExceptionState.RESOLVED,
            expected_version=problem.version,
            note="XML recebido e conferido.",
        )
        assert problem.state == ExceptionState.RESOLVED
        accept_tasks(setup)
        states.append(advance(setup, CycleState.APPROVAL))
    return link, states, versions


def _publish_and_complete(
    setup: PortalSetup,
    link: EvidenceLink,
    states: list[str],
    versions: list[int],
    peer: FakeSMTP,
) -> EvidenceScenario:
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        proposal = propose(
            setup.graph.staff,
            setup.cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CLIENT_PUBLICATION,
                    step_key="scenario-a",
                    recipient_ids=(setup.contact.pk,),
                    document_link_ids=(link.pk,),
                    evidence_request_ids=(setup.request.pk,),
                    observation_ids=tuple(
                        SourceObservation.objects.filter(
                            client=setup.graph.client, period=setup.cycle.period_start
                        ).values_list("pk", flat=True)
                    ),
                    subject="Evidência conferida",
                    body="Confira a nota do período.",
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=setup.cycle.version,
        )
        proposal = approve(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        ).proposal
        states.append(advance(setup, CycleState.EXECUTION))
        result = execute(
            setup.graph.actor, proposal.pk, expected_version=proposal.version
        )
        assert result.executed
        publication = ClientPublication.objects.get(proposal=proposal)
    assert dispatch_tenant(str(setup.graph.tenant.pk)) == 1
    assert len(peer.received) == 2
    response = setup.portal().post(
        f"/publicacoes/{publication.pk}/",
        {"publication_digest": publication.publication_digest},
    )
    assert response.status_code == 200
    with tenant_context(setup.graph.tenant.pk):
        close_cycle(CompletionSetup(setup.graph, setup.cycle))
        setup.cycle.refresh_from_db()
        states.append(setup.cycle.state)
        generation = CompletionGeneration.objects.get(cycle=setup.cycle)
        return EvidenceScenario(setup, generation.index, tuple(states), tuple(versions))
