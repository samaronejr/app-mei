"""Real selected portal recipient, staff author and independent firm approver."""

from dataclasses import dataclass
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientAssignment, ClientContact
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import ActionProposal, ServiceCycle
from apps.cycles.services.approvals import approve, propose
from apps.cycles.services.cycle import open_cycle
from apps.tenants.models import Membership, TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.support import enrol_totp
from tests.ui.factories import add_member

HOSTILE_BODY = "<script>window.T16_CANARY=true</script>\nDocumentos para conferência."


@dataclass(frozen=True, slots=True)
class PublicationGraph:
    graph: CycleSetup
    cycle: ServiceCycle
    contact: ClientContact
    portal_user: User
    proposal: ActionProposal

    def session(self, *, portal: bool = True, user: User | None = None) -> Client:
        account = user or (self.portal_user if portal else self.graph.actor)
        suffix = "-portal" if portal else ""
        host = f"{self.graph.tenant.slug}{suffix}.localhost"
        client = Client(SERVER_NAME=host)
        cookie = mfa_session_cookie(account, f"http://{host}/")
        assert "name" in cookie
        assert "value" in cookie
        client.cookies[cookie["name"]] = cookie["value"]
        return client


def publication_graph(
    slug: str = "publication", *, role: str = TenantRole.CLIENT_OWNER
) -> PublicationGraph:
    graph = cycle_setup(slug)
    enrol_totp(graph.actor)
    enrol_totp(graph.staff)
    portal_user = add_member(
        graph.tenant, f"client@{slug}.test", role, client=graph.client
    )
    with tenant_context(graph.tenant.pk):
        ClientAssignment.objects.create(
            tenant=graph.tenant, client=graph.client, user=graph.staff
        )
        member = Membership.objects.get(
            tenant=graph.tenant, client=graph.client, user=portal_user
        )
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            membership=member,
            name="Pessoa do cliente",
            email=portal_user.email,
            channel_email_allowed=True,
        )
        ref = open_cycle(
            graph.actor,
            graph.engagement.pk,
            PERIOD,
            graph.definition.pk,
            idempotency_key=slug,
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        proposal = propose(
            graph.staff,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.CLIENT_PUBLICATION,
                    step_key="publication",
                    recipient_ids=(contact.pk,),
                    subject="Documentos de setembro",
                    body=HOSTILE_BODY,
                ),
                timezone.now() + timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
        proposal = approve(graph.actor, proposal.pk, expected_version=1).proposal
        cycle.refresh_from_db()
    return PublicationGraph(graph, cycle, contact, portal_user, proposal)
