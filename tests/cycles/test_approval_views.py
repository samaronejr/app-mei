"""Real firm HTTP: frozen facts, scoped decisions, conflicts and owner policy."""

import json
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.authz.models import GrantLevel, RoleGrant
from apps.clients.maintenance import ContactData, save_contact
from apps.clients.models import ClientAssignment
from apps.clients.services import unassign_client
from apps.core.tenancy import tenant_context
from apps.cycles.models import ActionProposal, Approval, CycleEvent, ProposalState
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import ObservationKind, SourceObservation
from apps.security.csp import HEADER, build_policy
from apps.tenants.models import TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.cycles.approval_fixtures import ApprovalGraph, approval_graph
from tests.support import enrol_totp
from tests.ui.refusal_contract import assert_readable_page

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class Desk:
    """Two real authenticated firm sessions, not a bypassed request decorator."""

    setup: ApprovalGraph
    owner: Client
    staff: Client


@pytest.fixture
def desk(settings: SettingsWrapper) -> Desk:
    settings.ALLOWED_HOSTS = [".localhost", "localhost", "testserver"]
    setup = approval_graph("approval-http")
    host = f"{setup.graph.tenant.slug}.localhost"
    owner, staff = Client(SERVER_NAME=host), Client(SERVER_NAME=host)
    for user, session in ((setup.graph.actor, owner), (setup.graph.staff, staff)):
        enrol_totp(user)
        cookie = mfa_session_cookie(user, f"http://{host}/")
        session.cookies[cookie["name"]] = cookie["value"]
    return Desk(setup, owner, staff)


def test_card_is_a_read_only_view_of_frozen_facts_before_decisions(desk: Desk) -> None:
    setup = desk.setup
    with tenant_context(setup.graph.tenant.pk):
        before = bytes(setup.proposal.manifest_bytes)
        save_contact(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            contact_id=setup.contact.pk,
            data=ContactData(name="Changed label", email="changed@example.test"),
            expected_version=setup.contact.version,
        )
        event_count = CycleEvent.objects.count()
    response = desk.owner.get(f"/acoes/{setup.proposal.pk}/")
    assert response.status_code == 200
    assert response.headers[HEADER] == build_policy()
    html = response.content.decode()
    assert "recipient@example.test" in html
    assert "changed@example.test" not in html
    assert html.index('id="frozen-recipients"') < html.index("data-decision-controls")
    assert html.index('id="frozen-policy"') < html.index("data-decision-controls")
    with tenant_context(setup.graph.tenant.pk):
        setup.proposal.refresh_from_db()
        assert bytes(setup.proposal.manifest_bytes) == before
        assert setup.proposal.state == ProposalState.AWAITING
        assert CycleEvent.objects.count() == event_count


def test_card_names_each_frozen_recipient_beside_its_address(desk: Desk) -> None:
    html = desk.owner.get(f"/acoes/{desk.setup.proposal.pk}/").content.decode()
    section = html[
        html.index('id="frozen-recipients"') : html.index('id="frozen-message"')
    ]
    assert desk.setup.contact.name in section
    assert "recipient@example.test" in section


def test_approve_post_persists_exact_decision_and_stale_post_is_409(desk: Desk) -> None:
    setup = desk.setup
    url = f"/acoes/{setup.proposal.pk}/"
    assert desk.owner.get(url).status_code == 200
    accepted = desk.owner.post(url, {"action": "approve", "expected_version": 1})
    assert accepted.status_code == 302
    with tenant_context(setup.graph.tenant.pk):
        decision = Approval.objects.get(proposal=setup.proposal)
        assert decision.digest == setup.proposal.digest
        before = CycleEvent.objects.count()
    stale = desk.owner.post(
        url, {"action": "reject", "reason": "Stale refusal", "expected_version": 1}
    )
    assert stale.status_code == 409
    with tenant_context(setup.graph.tenant.pk):
        assert Approval.objects.filter(proposal=setup.proposal).count() == 1
        assert CycleEvent.objects.count() == before
        setup.proposal.refresh_from_db()
        assert setup.proposal.state == ProposalState.APPROVED


def test_denied_capability_and_revoked_portfolio_never_write_decisions(
    desk: Desk,
) -> None:
    setup = desk.setup
    url = f"/acoes/{setup.proposal.pk}/"
    denied = desk.staff.post(url, {"action": "approve", "expected_version": 1})
    assert denied.status_code == 403
    with tenant_context(setup.graph.tenant.pk):
        assignment = ClientAssignment.objects.get(
            client=setup.graph.client, user=setup.graph.staff
        )
        unassign_client(actor=setup.graph.actor, assignment=assignment)
        before = CycleEvent.objects.count()
    assert desk.staff.get(url).status_code == 404
    assert (
        desk.staff.post(url, {"action": "cancel", "expected_version": 1}).status_code
        == 404
    )
    with tenant_context(setup.graph.tenant.pk):
        assert not Approval.objects.exists()
        assert CycleEvent.objects.count() == before
    read_grant = RoleGrant.objects.get(
        role=TenantRole.OWNER, capability__slug="clients.view_assigned"
    )
    read_grant.level = GrantLevel.NONE
    read_grant.save(update_fields=["level"])
    denied_read = desk.owner.get(url)
    assert denied_read.status_code == 404
    assert b"recipient@example.test" not in denied_read.content


def test_create_form_uses_real_selected_ids_and_escapes_frozen_text(desk: Desk) -> None:
    setup = desk.setup
    url = f"/ciclos/{setup.cycle.pk}/propor/"
    body = "<script>window.approvalInjected = true</script>"
    response = desk.staff.post(
        url,
        {
            "expected_version": setup.cycle.version,
            "action_kind": "email_notice",
            "step_key": "http-notice",
            "occurrence": 1,
            "recipients": [str(setup.contact.pk)],
            "subject": "Synthetic subject",
            "body": body,
            "expires_at": timezone.localtime(
                timezone.now() + timedelta(hours=2)
            ).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert response.status_code == 302
    card = desk.owner.get(response.headers["Location"])
    assert card.status_code == 200
    html = card.content.decode()
    assert "<script>window.approvalInjected" not in html
    assert "&lt;script&gt;window.approvalInjected" in html
    with tenant_context(setup.graph.tenant.pk):
        created = ActionProposal.objects.exclude(pk=setup.proposal.pk).get()
        frozen = json.loads(bytes(created.manifest_bytes))
        assert frozen["payload"]["body"] == body
        assert frozen["recipients"][0]["contact_id"] == str(setup.contact.pk)
        assert not Approval.objects.filter(proposal=created).exists()


def test_a_refused_decision_reads_in_plain_sentences(desk: Desk) -> None:
    """AXR1-054: a refusal without a reason is the service's sentence on the card."""
    response = desk.owner.post(
        f"/acoes/{desk.setup.proposal.pk}/",
        {"action": "reject", "reason": "", "expected_version": 1},
    )
    assert_readable_page(
        response,
        status=HTTPStatus.UNPROCESSABLE_CONTENT,
        reason="Informe o motivo para não aprovar.",
    )


def test_a_refused_proposal_reads_in_plain_sentences(desk: Desk) -> None:
    """AXR1-054: propose's ValidationError is a form error in sentences."""
    setup = desk.setup
    response = desk.staff.post(
        f"/ciclos/{setup.cycle.pk}/propor/",
        {
            "expected_version": setup.cycle.version,
            "action_kind": "email_notice",
            "step_key": "http-notice-expired",
            "occurrence": 1,
            "recipients": [str(setup.contact.pk)],
            "subject": "Synthetic subject",
            "body": "Synthetic body",
            "expires_at": timezone.localtime(
                timezone.now() - timedelta(hours=2)
            ).strftime("%Y-%m-%dT%H:%M"),
        },
    )
    assert_readable_page(
        response,
        status=HTTPStatus.UNPROCESSABLE_CONTENT,
        reason="Informe uma validade futura e um início anterior ao vencimento.",
    )


def test_create_form_offers_the_period_calendar_observation(desk: Desk) -> None:
    setup = desk.setup
    with tenant_context(setup.graph.tenant.pk):
        generate_das_calendar(
            setup.graph.client, starting_from=setup.cycle.period_start
        )
        calendar = SourceObservation.objects.get(
            client=setup.graph.client,
            period=setup.cycle.period_start,
            kind=ObservationKind.CALENDAR_SCHEDULED,
        )
    response = desk.staff.get(f"/ciclos/{setup.cycle.pk}/propor/")
    assert response.status_code == 200
    assert f'value="{calendar.pk}"' in response.content.decode()


def test_malformed_revision_link_is_a_422_not_a_server_error(desk: Desk) -> None:
    response = desk.owner.get(
        f"/ciclos/{desk.setup.cycle.pk}/propor/", {"revisao_de": "not-a-uuid"}
    )
    assert response.status_code == 422


def test_policy_route_is_owner_only_and_versioned(desk: Desk) -> None:
    assert desk.staff.get("/gestao/aprovacoes/").status_code == 403
    assert desk.owner.get("/gestao/aprovacoes/").status_code == 200
    saved = desk.owner.post("/gestao/aprovacoes/", {"expected_version": 1, "quorum": 2})
    assert saved.status_code == 302
    stale = desk.owner.post("/gestao/aprovacoes/", {"expected_version": 1, "quorum": 1})
    assert stale.status_code == 409
    desk.setup.graph.tenant.refresh_from_db()
    assert desk.setup.graph.tenant.approval_quorum == 2


def test_approval_card_read_has_a_bounded_query_cost(desk: Desk) -> None:
    url = f"/acoes/{desk.setup.proposal.pk}/"
    assert desk.owner.get(url).status_code == 200
    with CaptureQueriesContext(connection) as queries:
        response = desk.owner.get(url)
    assert response.status_code == 200
    assert len(queries) <= 25, [query["sql"] for query in queries.captured_queries]
