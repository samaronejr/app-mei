"""Real receipts, contacts and obligations for cycle-detail read controls."""

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from django.utils import timezone

from apps.clients.models import ClientAssignment, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.completion_checks import valid_waiver
from apps.cycles.effects.executors import execute
from apps.cycles.models import CycleState, CycleTask, RequestState, TaskState
from apps.cycles.openers import REQUEST_OVERDUE
from apps.cycles.services.approvals import approve
from apps.cycles.services.cycle import add_note
from apps.cycles.services.proposed_transitions import propose_waiver
from apps.cycles.services.requests import RequestFields, create_request
from apps.obligations.models import Obligation, ObligationType, Periodicity
from apps.tenants.models import Invite, TenantRole
from tests.cycles.test_exceptions import _open
from tests.portal.request_fixtures import portal_setup

if TYPE_CHECKING:
    from tests.portal.request_fixtures import PortalSetup


def add_waivers(world: "PortalSetup", start: int, count: int) -> set[UUID]:
    added: set[UUID] = set()
    for index in range(start, start + count):
        world.cycle.refresh_from_db()
        request = create_request(
            world.graph.actor,
            world.cycle.pk,
            RequestFields(f"Waived request {index}"),
            expected_version=world.cycle.version,
        )
        proposal = propose_waiver(
            world.graph.staff,
            request.pk,
            reason=f"Not applicable to this period: {index}.",
            expected_version=request.version,
        )
        proposal = approve(
            world.graph.actor, proposal.pk, expected_version=proposal.version
        ).proposal
        assert execute(
            world.graph.actor, proposal.pk, expected_version=proposal.version
        ).executed
        request.refresh_from_db()
        assert request.state == RequestState.WAIVED
        assert request.required
        assert valid_waiver(request)
        added.add(request.pk)
    return added


def add_contacts(world: "PortalSetup", start: int, count: int) -> set[UUID]:
    added: set[UUID] = set()
    for index in range(start, start + count):
        contact = ClientContact.objects.create(
            tenant=world.graph.tenant,
            client=world.graph.client,
            name=f"Invited contact {index}",
            email=f"contact-{index}@{world.graph.tenant.slug}.test",
        )
        # Two live rows exercise newest-invitation selection as well as growth.
        for offset in range(2):
            invitation, _ = Invite.issue(
                tenant=world.graph.tenant,
                client=world.graph.client,
                email=contact.email,
                role=TenantRole.CLIENT_OWNER,
                expires_at=timezone.now() + timedelta(days=7),
            )
            # The fixed test clock must not leave newest selection tied.
            Invite.objects.filter(pk=invitation.pk).update(
                created_at=timezone.now() + timedelta(minutes=offset)
            )
        added.add(contact.pk)
    return added


def add_obligations(world: "PortalSetup", start: int, count: int) -> set[UUID]:
    added: set[UUID] = set()
    for index in range(start, start + count):
        kind = ObligationType.objects.create(
            code=f"T36-{index}",
            name=f"Distinct obligation type {index}",
            periodicity=Periodicity.MONTHLY,
        )
        obligation = Obligation.objects.create(
            tenant=world.graph.tenant,
            client=world.graph.client,
            obligation_type=kind,
            competence_month=world.cycle.period_start,
            nominal_due_date=world.cycle.period_end,
            resolved_due_date=world.cycle.period_end,
        )
        added.add(obligation.pk)
    return added


def populated_detail(slug: str) -> "PortalSetup":
    """Populate all closure categories, with one genuinely executed waiver."""
    world = portal_setup(slug)
    with tenant_context(world.graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=world.graph.tenant,
            client=world.graph.client,
            user=world.graph.staff,
        )
        world.cycle.state = CycleState.EXECUTION
        world.cycle.save(update_fields=["state"])
        CycleTask.objects.filter(cycle=world.cycle).update(state=TaskState.ACCEPTED)
        task = CycleTask.objects.filter(cycle=world.cycle).first()
        assert task is not None
        task.state = TaskState.READY
        task.save(update_fields=["state"])
        add_waivers(world, 0, 1)
        add_contacts(world, 0, 1)
        add_obligations(world, 0, 1)
        _open(world.graph, world.cycle, REQUEST_OVERDUE, world.request.pk)
        propose_waiver(
            world.graph.staff,
            world.request.pk,
            reason="Awaiting an independent decision.",
            expected_version=world.request.version,
        )
        world.cycle.refresh_from_db()
        add_note(
            world.graph.actor,
            world.cycle.pk,
            "PRIVATE-T36-DISPLAY-NOTE",
            expected_version=world.cycle.version,
        )
        world.cycle.refresh_from_db()
    return world
