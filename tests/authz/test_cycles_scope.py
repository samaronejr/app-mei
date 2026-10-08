"""ASSIGNED portfolio scope on the cycles surface (apps/authz/portfolio.py:92-113).

A staff accountant's portfolio is their assignment list, not the firm. This file
drives the four cycle-surface views the plan names — cycle detail, the request
action endpoint, document review, and the approval card — for a client on the
actor's list and a client off it. Off-list answers are 404 (load_visible_client
makes invisible mean nonexistent), on-list answers are 200, and a firm-wide role
sees both.
"""

import datetime as dt
from dataclasses import dataclass
from http import HTTPStatus
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.clients.models import (
    ClientCompany,
    ClientContact,
    Engagement,
    TaxProfile,
)
from apps.core.tenancy import tenant_context
from apps.cycles.effects.inputs import ProposalDraft, ProposalInput
from apps.cycles.effects.manifest import ActionKind
from apps.cycles.models import (
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    ServiceCycle,
)
from apps.cycles.packs import install_system_packs
from apps.cycles.services.approvals import propose
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.requests import RequestFields, create_request
from apps.obligations.models import Document
from apps.tenants.models import Tenant
from tests.cycles.cycle_fixtures import PERIOD
from tests.isolation.rolecheck import assert_isolated_role
from tests.ui.factories import assign, make_firm

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass
class Surface:
    """One client's worth of the objects the four views address by pk."""

    cycle: ServiceCycle
    request: EvidenceRequest
    link: EvidenceLink
    proposal_pk: object


def _surface(tenant: Tenant, actor: User, client: ClientCompany) -> Surface:
    """A live cycle, request, review link and proposal under `client`."""
    with tenant_context(tenant.id):
        engagement = Engagement.all_objects.create(
            tenant=tenant,
            client=client,
            service_kind="mei_mensal",
            starts_on=PERIOD,
            responsible=actor,
            provenance="staff_entry",
        )
        TaxProfile.all_objects.create(
            tenant=tenant,
            client=client,
            regime="mei",
            valid_from=PERIOD,
            provenance="staff_entry",
        )
        definition = __import__(
            "apps.cycles.models", fromlist=["PlaybookVersion"]
        ).PlaybookVersion.all_objects.get(playbook__key="mei-mensal", state="published")
        ref = open_cycle(
            actor,
            engagement.pk,
            PERIOD,
            definition.pk,
            idempotency_key=uuid4().hex,
        )
        cycle = ServiceCycle.all_objects.get(pk=ref.id)
        request = create_request(
            actor,
            cycle.pk,
            RequestFields(title="varredura"),
            expected_version=cycle.version,
        )
        contact = ClientContact.all_objects.create(
            tenant=tenant,
            client=client,
            name="Destinatario",
            email=f"dest-{uuid4().hex[:8]}@example.test",
        )
        document = Document.all_objects.create(
            tenant=tenant,
            client=client,
            sha256="c" * 64,
            original_filename="doc.pdf",
            content_type="application/pdf",
            byte_size=10,
            uploaded_by=actor,
        )
        link = EvidenceLink.all_objects.create(
            tenant=tenant,
            client=client,
            request=request,
            document=document,
            origin="portal",
            submitted_by=actor,
        )
        DocumentIntake.all_objects.create(
            tenant=tenant,
            client=client,
            document=document,
            reason="recebido",
        )
        cycle.refresh_from_db()
        proposal = propose(
            actor,
            cycle.pk,
            ProposalDraft(
                ProposalInput(
                    action_kind=ActionKind.EMAIL_NOTICE,
                    step_key="notice",
                    recipient_ids=(contact.pk,),
                    subject="aviso",
                    body="corpo",
                ),
                timezone.now() + dt.timedelta(days=1),
            ),
            expected_version=cycle.version,
        )
        # propose bumps cycle.version inside its transaction; the in-memory row
        # is stale for any later optimistic-lock POST.
        cycle.refresh_from_db()
    return Surface(cycle, request, link, proposal.pk)


def _responses(client: Client, surface: Surface) -> dict[str, int]:
    """The four named views, addressed by this client's object ids."""
    return {
        "cycle-detail": client.get(
            reverse("cycle-detail", kwargs={"pk": surface.cycle.pk})
        ).status_code,
        "cycle-request-action": client.post(
            reverse("cycle-request-action", kwargs={"pk": surface.cycle.pk}),
            # 'update' on the seeded request with its own version reaches the
            # service; 'send' would refuse in-domain (no drafts / no eligible
            # recipients) and confuse a denial with a 422.
            {
                "action": "update",
                "request_id": str(surface.request.pk),
                "expected_version": surface.request.version,
                "title": "varredura",
                "kind": "documento",
            },
        ).status_code,
        "document-review": client.get(
            reverse("document-review", kwargs={"pk": surface.link.pk})
        ).status_code,
        "approval-card": client.get(
            reverse("approval-card", kwargs={"pk": surface.proposal_pk})
        ).status_code,
    }


def test_assigned_scope_denies_and_allows_the_named_views() -> None:
    # Given a firm whose staff accountant holds an assignment to only one client
    assert_isolated_role()
    firm = make_firm("axr-scope", client_count=2)
    firm.tenant.features = ["cycles_r1", "import_r1"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.id):
        install_system_packs(firm.tenant)
    assigned_client, off_client = firm.clients[0], firm.clients[1]
    assign(firm, assigned_client, firm.accountant)

    with tenant_context(firm.tenant.id):
        on = _surface(firm.tenant, firm.owner, assigned_client)
        off = _surface(firm.tenant, firm.owner, off_client)

    # When the staff actor hits each named surface for each client
    client = firm.as_accountant()
    for response in _responses(client, on).items():
        # Then the assigned client renders — POST actions answer FOUND on the
        # same 302 any successful request command returns.
        assert response[1] in {HTTPStatus.OK, HTTPStatus.FOUND}, (
            f"{response[0]} answered {response[1]} for an assigned client"
        )
    for name, status in _responses(client, off).items():
        # And the unassigned client answers 404 — the object exists, but not for
        # this actor, and 404 is the only answer that does not say so
        assert status == HTTPStatus.NOT_FOUND, (
            f"{name} answered {status} for an unassigned client"
        )


def test_firm_wide_roles_see_both_clients() -> None:
    # Given the same two-client firm
    assert_isolated_role()
    firm = make_firm("axr-scope-wide", client_count=2)
    firm.tenant.features = ["cycles_r1", "import_r1"]
    firm.tenant.save(update_fields=["features"])
    with tenant_context(firm.tenant.id):
        install_system_packs(firm.tenant)
        surfaces = [
            _surface(firm.tenant, firm.owner, client) for client in firm.clients
        ]

    # When the OWNER (firm-wide scope) hits both clients' surfaces
    client = firm.as_owner()
    for surface in surfaces:
        for name, status in _responses(client, surface).items():
            # Then everything renders: only ASSIGNED narrows the portfolio, and
            # the owner's scope is the whole firm
            assert status in {HTTPStatus.OK, HTTPStatus.FOUND}, (
                f"{name} answered {status} for the owner's firm-wide scope"
            )
