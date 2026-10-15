"""A real firm, cycle, requests and signed-in portal session for the T11 suites.

No mocked boundary: requests are created and sent through the firm-side services,
the portal session passes the real middleware (role switch, RLS, MFA session), and
a second client with its own open request is the foreign-id control.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import ClientCompany, ClientContact, Engagement
from apps.core.tenancy import tenant_context
from apps.cycles.models import (
    EvidenceRequest,
    RequestInternal,
    RequestState,
    ServiceCycle,
)
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.tenants.models import TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import PERIOD, CycleSetup, cycle_setup
from tests.support import enrol_totp
from tests.ui.factories import add_member

INTERNAL_CANARY = "INTERNAL-INSTRUCTION-CANARY"
OTHER_CLIENT_CANARY = "OTHER-CLIENT-REQUEST-CANARY"


@dataclass
class PortalSetup:
    """One firm, two clients with an open request each, a seat in the first."""

    graph: CycleSetup
    cycle: ServiceCycle
    request: EvidenceRequest
    other_client: ClientCompany
    other_request: EvidenceRequest
    portal_user: User
    contact: ClientContact

    @property
    def portal_host(self) -> str:
        """The firm's portal host."""
        return f"{self.graph.tenant.slug}-portal.localhost"

    def portal(self, user: User | None = None) -> Client:
        """A portal session carrying the MFA state a real sign-in writes."""
        account = user or self.portal_user
        browser = Client(SERVER_NAME=self.portal_host)
        cookie = mfa_session_cookie(account, f"http://{self.portal_host}/")
        browser.cookies[cookie["name"]] = cookie["value"]
        return browser

    def firm(self, user: User | None = None) -> Client:
        """A firm-host session for the owner (or `user`)."""
        account = user or self.graph.actor
        host = f"{self.graph.tenant.slug}.localhost"
        browser = Client(SERVER_NAME=host)
        cookie = mfa_session_cookie(account, f"http://{host}/")
        browser.cookies[cookie["name"]] = cookie["value"]
        return browser

    def reload(self, request: EvidenceRequest) -> EvidenceRequest:
        """Re-read a request inside the firm's tenant context."""
        with tenant_context(self.graph.tenant.pk):
            return EvidenceRequest.objects.get(pk=request.pk)


def upload(
    name: str, payload: bytes, content_type: str = "application/pdf"
) -> SimpleUploadedFile:
    """A multipart file for the Django test client."""
    return SimpleUploadedFile(name, payload, content_type=content_type)


def _foreign_request(graph: CycleSetup) -> tuple[ClientCompany, EvidenceRequest]:
    other = ClientCompany.objects.create(
        tenant=graph.tenant,
        legal_name="Other client",
        cnpj="11444777000161",
        status="active",
    )
    engagement = Engagement.objects.create(
        tenant=graph.tenant,
        client=other,
        service_kind="mei_mensal",
        starts_on=PERIOD,
        provenance="staff_entry",
    )
    cycle = ServiceCycle.objects.create(
        tenant=graph.tenant,
        client=other,
        engagement=engagement,
        playbook_id=graph.definition.playbook_id,
        playbook_version=graph.definition,
        period_start=PERIOD,
        period_end=PERIOD,
        state="planejado",
    )
    request = EvidenceRequest.objects.create(
        tenant=graph.tenant,
        client=other,
        cycle=cycle,
        title=OTHER_CLIENT_CANARY,
        state=RequestState.REQUESTED,
        created_by=graph.actor,
        requested_at=timezone.now(),
    )
    RequestInternal.objects.create(tenant=graph.tenant, client=other, request=request)
    return other, request


def portal_setup(slug: str = "t11-portal") -> PortalSetup:
    """Build the graph; the first request is SENT through the real services."""
    graph = cycle_setup(slug)
    enrol_totp(graph.actor)
    enrol_totp(graph.staff)
    portal_user = add_member(
        graph.tenant,
        f"dona@{slug}.test",
        TenantRole.CLIENT_OWNER,
        client=graph.client,
    )
    with tenant_context(graph.tenant.pk):
        ref = open_cycle(
            graph.actor,
            graph.engagement.pk,
            PERIOD,
            graph.definition.pk,
            idempotency_key="t11",
        )
        contact = ClientContact.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            name="Dona Maria",
            email=portal_user.email,
        )
        request = create_request(
            graph.actor,
            ref.id,
            RequestFields(
                title="Notas de serviço de setembro",
                client_note="Precisamos das notas emitidas em setembro.",
                due_on=timezone.localdate() + timedelta(days=5),
                instructions=INTERNAL_CANARY,
            ),
            expected_version=ref.version,
        )
        cycle = ServiceCycle.objects.get(pk=ref.id)
        send_requests(graph.actor, cycle.pk, expected_version=cycle.version)
        cycle.refresh_from_db()
        request.refresh_from_db()
        other_client, other_request = _foreign_request(graph)
    return PortalSetup(
        graph,
        cycle,
        request,
        other_client,
        other_request,
        portal_user,
        contact,
    )
