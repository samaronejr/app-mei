"""T42: client actions, calendar facts and coverage stay separate over real HTTP.

Initial cases were authored before implementation; none ran during source-only prep.
The signed-in client passes PortalMiddleware and its app_portal role switch.
Only the clock is pinned. Database state and request projections are real.
"""

from datetime import date, timedelta
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.evidence import ReviewDecision
from apps.cycles.models import CycleState, EvidenceRequest, RequestState, ServiceCycle
from apps.cycles.services.cycle import open_cycle
from apps.cycles.services.review import record_review
from apps.obligations.models import Obligation, ObligationStatus, ObligationType
from apps.tenants.models import TenantRole
from tests.browser.harness import mfa_session_cookie
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.cycles.document_fixtures import reviewed
from tests.portal.request_fixtures import (
    INTERNAL_CANARY,
    OTHER_CLIENT_CANARY,
    PortalSetup,
    portal_setup,
    upload,
)
from tests.ui.factories import add_member

pytestmark = pytest.mark.django_db(transaction=True)
TODAY = date(2026, 10, 12)


class Markup(HTMLParser):
    """Collect rendered attributes, not translated sentences or template source."""

    def __init__(self, body: bytes) -> None:
        super().__init__()
        self.elements: list[dict[str, str | None]] = []
        self.feed(body.decode())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(dict(attrs))

    def one(self, attribute: str, value: str) -> dict[str, str | None]:
        matches = [row for row in self.elements if row.get(attribute) == value]
        assert len(matches) == 1, (attribute, value, matches)
        return matches[0]


@pytest.fixture(autouse=True)
def _portal_surface(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings.ALLOWED_HOSTS = ["testserver", ".localhost", "localhost"]
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setattr("apps.portal.middleware.PORTAL_URLCONF", "apps.portal.urls")
    monkeypatch.setattr("django.utils.timezone.localdate", lambda: TODAY)


def _home(setup: PortalSetup) -> Markup:
    """Capture role-scoped SQL as well as the actual rendered home."""
    browser = setup.portal()
    with CaptureQueriesContext(connection) as queries:
        response = browser.get("/")
    assert response.status_code == HTTPStatus.OK
    statements = [query["sql"] for query in queries]
    role_index = statements.index("SET LOCAL ROLE app_portal")
    commit_index = statements.index("COMMIT", role_index)
    portal_sql = " ".join(statements[role_index:commit_index])
    assert "cycles_evidencerequest" in portal_sql
    for table in (
        "cycles_requestinternal",
        "cycles_internalnote",
        "cycles_review",
        "obligations_sourceassertion",
    ):
        assert table not in portal_sql
    body = response.content.decode()
    assert INTERNAL_CANARY not in body
    assert OTHER_CLIENT_CANARY not in body
    assert str(setup.other_request.pk) not in body
    return Markup(response.content)


def _due(setup: PortalSetup, offset: int | None) -> None:
    with tenant_context(setup.graph.tenant.pk):
        EvidenceRequest.objects.filter(pk=setup.request.pk).update(
            due_on=TODAY + timedelta(days=offset) if offset is not None else None
        )


def _calendar(setup: PortalSetup, offset: int, status: str) -> Obligation:
    with tenant_context(setup.graph.tenant.pk):
        return Obligation.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            obligation_type=ObligationType.objects.get(code="DAS"),
            competence_month=PERIOD,
            nominal_due_date=TODAY + timedelta(days=offset),
            resolved_due_date=TODAY + timedelta(days=offset),
            status=status,
        )


@pytest.mark.parametrize(
    ("state", "offset", "cycle_state", "status"),
    [
        (RequestState.REQUESTED, -5, CycleState.ACTIVE, "atrasada"),
        (RequestState.REQUESTED, 0, CycleState.WAITING, "solicitado"),
        (RequestState.REQUESTED, 5, CycleState.ACTIVE, "solicitado"),
        (RequestState.REQUESTED, None, CycleState.ACTIVE, "solicitado"),
        (RequestState.REJECTED, 5, CycleState.ACTIVE, "rejeitado"),
    ],
)
def test_actionable_request_controls_hero_primary_action_and_cycle(
    state: str, offset: int | None, cycle_state: str, status: str
) -> None:
    # Given an actionable request and no tracked obligations.
    setup = portal_setup("next-action")
    _due(setup, offset)
    with tenant_context(setup.graph.tenant.pk):
        EvidenceRequest.objects.filter(pk=setup.request.pk).update(state=state)
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(state=cycle_state)
        assert not Obligation.objects.exists()

    # When the client opens the real portal home.
    markup = _home(setup)

    # Then the same actionable request drives the hero, CTA and cycle instruction.
    assert markup.one("id", "portal-status")["data-status"] == "action_required"
    primary = markup.one("aria-labelledby", "proxima-acao")
    assert primary["data-action-kind"] == "request"
    link = markup.one("data-primary-action", "request")
    assert link["href"] == f"/solicitacoes/{setup.request.pk}/"
    assert link["data-primary-request-id"] == str(setup.request.pk)
    assert link["data-status"] == status
    cycle = markup.one("data-cycle-id", str(setup.cycle.pk))
    assert cycle["data-action-kind"] == "request"
    assert markup.one("data-coverage-state", "unknown")
    assert not any(
        "badge-ok" in (row.get("class") or "").split() for row in markup.elements
    )


def test_overdue_request_precedes_upcoming_request_and_calendar() -> None:
    # Given an overdue request, an upcoming one, and an overdue calendar fact.
    setup = portal_setup("next-priority")
    _due(setup, -5)
    with tenant_context(setup.graph.tenant.pk):
        upcoming = EvidenceRequest.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            cycle=setup.cycle,
            title="UPCOMING-REQUEST",
            due_on=TODAY + timedelta(days=1),
            state=RequestState.REQUESTED,
            created_by=setup.graph.actor,
        )
    _calendar(setup, -10, ObligationStatus.SCHEDULED)

    # When the client opens the portal.
    markup = _home(setup)

    # Then request work takes precedence without losing the scoped calendar fact.
    assert markup.one("data-primary-action", "request")[
        "data-primary-request-id"
    ] == str(setup.request.pk)
    assert markup.one("data-request-id", str(upcoming.pk))
    summary = markup.one("id", "legacy-obligations")
    assert summary["data-overdue-count"] == "1"
    assert summary["data-fact-kind"] == "calendar"
    assert summary["data-source-kind"] == "legacy_status"


def test_cycle_detail_offers_the_actionable_request_instead_of_waiting() -> None:
    # Given an active cycle whose request is still overdue.
    setup = portal_setup("next-cycle")
    _due(setup, -5)
    with tenant_context(setup.graph.tenant.pk):
        ServiceCycle.objects.filter(pk=setup.cycle.pk).update(state=CycleState.ACTIVE)

    # When the client opens that cycle.
    response = setup.portal().get(f"/ciclos/{setup.cycle.pk}/")

    # Then the cycle's next step is a working request link, not a wait instruction.
    assert response.status_code == HTTPStatus.OK
    markup = Markup(response.content)
    assert (
        markup.one("aria-labelledby", "cycle-period")["data-action-kind"] == "request"
    )
    assert markup.one("data-primary-action", "request")["href"] == (
        f"/solicitacoes/{setup.request.pk}/"
    )
    assert INTERNAL_CANARY not in response.content.decode()


@pytest.mark.parametrize("has_cycle", [False, True])
def test_no_requests_is_neutral_and_does_not_invent_coverage(has_cycle: bool) -> None:
    # Given a real client seat with no requests or obligations, optionally a cycle.
    graph = cycle_setup("next-empty")
    user = add_member(
        graph.tenant,
        "client@next-empty.test",
        TenantRole.CLIENT_OWNER,
        client=graph.client,
    )
    if has_cycle:
        with tenant_context(graph.tenant.pk):
            open_cycle(
                graph.actor,
                graph.engagement.pk,
                PERIOD,
                graph.definition.pk,
                idempotency_key="empty",
            )
    host = f"{graph.tenant.slug}-portal.localhost"
    browser = Client(SERVER_NAME=host)
    cookie = mfa_session_cookie(user, f"http://{host}/")
    browser.cookies[cookie["name"]] = cookie["value"]

    # When the client opens the portal.
    response = browser.get("/")

    # Then no action is identified; absence is not verified fiscal coverage.
    assert response.status_code == HTTPStatus.OK
    markup = Markup(response.content)
    assert markup.one("id", "portal-status")["data-status"] == "no_client_action"
    assert markup.one("aria-labelledby", "proxima-acao")["data-action-kind"] == "none"
    assert markup.one("data-coverage-state", "unknown")
    assert markup.one("id", "legacy-obligations")["data-obligation-count"] == "0"
    assert not any(
        "badge-ok" in (row.get("class") or "").split() for row in markup.elements
    )


@pytest.mark.parametrize(
    "state", [RequestState.ACCEPTED, RequestState.WAIVED, RequestState.REPLACED]
)
def test_only_completed_requests_and_foreign_work_do_not_become_client_actions(
    state: str,
) -> None:
    # Given no actionable request here, but a real overdue request for another client.
    setup = portal_setup("next-completed")
    with tenant_context(setup.graph.tenant.pk):
        EvidenceRequest.objects.filter(pk=setup.request.pk).update(state=state)
        EvidenceRequest.objects.filter(pk=setup.other_request.pk).update(
            due_on=TODAY - timedelta(days=30)
        )
        assert EvidenceRequest.objects.filter(
            pk=setup.other_request.pk, state=RequestState.REQUESTED
        ).exists()

    # When the selected client opens the portal.
    markup = _home(setup)

    # Then the foreign request does not affect the hero or next action.
    assert markup.one("id", "portal-status")["data-status"] == "no_client_action"
    assert markup.one("aria-labelledby", "proxima-acao")["data-action-kind"] == "none"
    assert not any(
        row.get("data-primary-action") == "request" for row in markup.elements
    )


@pytest.mark.parametrize("state", [RequestState.REQUESTED, RequestState.REJECTED])
def test_submitted_or_resubmitted_request_waits_for_review_not_another_upload(
    state: str,
) -> None:
    # Given a counted upload through the real portal, even after a rejection.
    setup = portal_setup("next-submitted")
    _due(setup, -5)
    with tenant_context(setup.graph.tenant.pk):
        EvidenceRequest.objects.filter(pk=setup.request.pk).update(state=state)
    sent = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("resubmitted.pdf", b"%PDF-1.4 next action\n")},
    )
    assert sent.status_code == HTTPStatus.FOUND

    # When the client reopens the portal.
    markup = _home(setup)

    # Then a submission is pending review, not verified or an outstanding client action.
    assert markup.one("id", "portal-status")["data-status"] == "awaiting_review"
    assert markup.one("aria-labelledby", "proxima-acao")["data-action-kind"] == "review"
    assert markup.one("data-status", "recebido")
    assert (
        markup.one("data-cycle-id", str(setup.cycle.pk))["data-action-kind"] == "review"
    )
    assert not any(
        row.get("data-primary-action") == "request" for row in markup.elements
    )
    assert not any(
        "badge-ok" in (row.get("class") or "").split() for row in markup.elements
    )


def test_received_request_does_not_hide_another_actionable_request() -> None:
    # Given an earlier request already submitted and a later request still outstanding.
    setup = portal_setup("next-after-received")
    _due(setup, -5)
    sent = setup.portal().post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("received.pdf", b"%PDF-1.4 received request\n")},
    )
    assert sent.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        pending = EvidenceRequest.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            cycle=setup.cycle,
            title="NEXT-ACTION-CANARY",
            due_on=TODAY + timedelta(days=1),
            state=RequestState.REQUESTED,
            created_by=setup.graph.actor,
        )

    # When the client opens the portal.
    markup = _home(setup)

    # Then the received chip remains neutral and the other request drives the action.
    assert markup.one("id", "portal-status")["data-status"] == "action_required"
    assert markup.one("data-primary-action", "request")[
        "data-primary-request-id"
    ] == str(pending.pk)
    assert markup.one("data-status", "recebido")


@pytest.mark.parametrize("phase", ["rejected", "resubmitted", "accepted"])
def test_review_lifecycle_updates_the_existing_portal_next_action(phase: str) -> None:
    # Given a real reviewed upload, optionally followed by a new client submission.
    setup = portal_setup("next-review-lifecycle")
    _, link = reviewed(setup, "nfe_emitente_cliente.xml")
    with tenant_context(setup.graph.tenant.pk):
        record_review(
            setup.graph.actor,
            link.pk,
            decision=(
                ReviewDecision.ACCEPTED
                if phase == "accepted"
                else ReviewDecision.REJECTED
            ),
            client_reason="" if phase == "accepted" else "REVIEW-CLIENT-REASON",
            corrections=[],
            expected_version=setup.reload(setup.request).version,
        )
    if phase == "resubmitted":
        sent = setup.portal().post(
            f"/solicitacoes/{setup.request.pk}/enviar",
            {"file": upload("replacement.pdf", b"%PDF-1.4 replacement request\n")},
        )
        assert sent.status_code == HTTPStatus.FOUND

    # When the client reopens the same portal home after the live state changed.
    markup = _home(setup)

    # Then rejection asks for action, resubmission awaits review, acceptance is scoped.
    expected = {
        "rejected": ("action_required", "request"),
        "resubmitted": ("awaiting_review", "review"),
        "accepted": ("no_client_action", "none"),
    }
    hero, action = expected[phase]
    assert markup.one("id", "portal-status")["data-status"] == hero
    assert markup.one("aria-labelledby", "proxima-acao")["data-action-kind"] == action
    assert markup.one("data-coverage-state", "unknown")


def test_older_request_is_not_hidden_by_the_latest_cycle() -> None:
    # Given an overdue request in an older period and an active newer cycle without one.
    setup = portal_setup("next-older-period")
    _due(setup, -30)
    with tenant_context(setup.graph.tenant.pk):
        newest = ServiceCycle.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            engagement=setup.graph.engagement,
            playbook_id=setup.graph.definition.playbook_id,
            playbook_version=setup.graph.definition,
            period_start=TODAY.replace(day=1),
            period_end=TODAY,
            state=CycleState.ACTIVE,
        )

    # When the client opens the portal across both periods.
    markup = _home(setup)

    # Then the outstanding request wins; the newer cycle doesn't claim a global wait.
    assert markup.one("id", "portal-status")["data-status"] == "action_required"
    assert markup.one("data-primary-action", "request")[
        "data-primary-request-id"
    ] == str(setup.request.pk)
    assert (
        markup.one("data-cycle-id", str(setup.cycle.pk))["data-action-kind"]
        == "request"
    )
    assert markup.one("data-cycle-id", str(newest.pk))["data-action-kind"] == "office"


@pytest.mark.parametrize("offset", [-5, 5])
def test_known_calendar_action_remains_available_without_open_requests(
    offset: int,
) -> None:
    # Given only a completed request and one known unsettled calendar entry.
    setup = portal_setup("next-calendar")
    with tenant_context(setup.graph.tenant.pk):
        EvidenceRequest.objects.filter(pk=setup.request.pk).update(
            state=RequestState.ACCEPTED
        )
    obligation = _calendar(setup, offset, ObligationStatus.SCHEDULED)

    # When the client opens the portal.
    markup = _home(setup)

    # Then past and future calendar facts remain actions without inventing coverage.
    assert (
        markup.one("aria-labelledby", "proxima-acao")["data-action-kind"] == "calendar"
    )
    assert markup.one("data-primary-action", "calendar")["href"] == "/pagamentos/"
    assert markup.one("datetime", obligation.resolved_due_date.isoformat())
    assert markup.one("data-coverage-state", "unknown")
    assert markup.one("id", "legacy-obligations")["data-fact-kind"] == "calendar"


def test_legacy_paid_status_does_not_make_the_r1_hero_verified() -> None:
    # Given no open client request and a legacy paid row (no official verification).
    setup = portal_setup("next-manual")
    with tenant_context(setup.graph.tenant.pk):
        EvidenceRequest.objects.filter(pk=setup.request.pk).update(
            state=RequestState.ACCEPTED
        )
    _calendar(setup, -5, ObligationStatus.PAID)

    # When the client opens the portal.
    markup = _home(setup)

    # Then a manual legacy status is labelled separately, never promoted to green.
    assert markup.one("id", "portal-status")["data-status"] == "no_client_action"
    assert markup.one("id", "legacy-obligations")["data-source-kind"] == "legacy_status"
    assert markup.one("data-coverage-state", "unknown")
    assert not any(
        "badge-ok" in (row.get("class") or "").split() for row in markup.elements
    )
