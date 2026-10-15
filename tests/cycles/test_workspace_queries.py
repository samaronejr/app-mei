"""T35: complete typed workspace reads, live scope, and batched roster policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, assert_never
from uuid import UUID

import pytest
from django.core.serializers import serialize
from django.db import connection
from django.db.models import Q
from django.http import Http404
from django.test.utils import CaptureQueriesContext

from apps.accounts.views import AuthenticatedRequest
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.models import Capability, GrantLevel, RoleGrant, UserCapabilityGrant
from apps.authz.portfolio import (
    VIEW_ALL,
    PortfolioScope,
    assignable_members,
    portfolio_scope,
)
from apps.clients.models import (
    ClientAssignment,
    ClientContact,
    Engagement,
    OnboardingItem,
    TaxProfile,
)
from apps.core.tenancy import tenant_context
from apps.cycles.exception_context import (
    ExceptionSelection,
    exception_rows,
    present_exceptions,
    read_exceptions,
)
from apps.cycles.models import (
    CycleState,
    ExceptionState,
    PlaybookVersion,
    ServiceCycle,
    VersionState,
)
from apps.cycles.models import Exception as CycleException
from apps.cycles.openers import REQUEST_OVERDUE
from apps.cycles.services.exceptions import transition_exception
from apps.cycles.services.requests import RequestFields, create_request
from apps.cycles.workspace import get_cycle_context, timeline
from apps.cycles.workspace_queries import WorkspaceRead, read_workspace
from apps.cycles.workspace_sections import period_requests
from apps.delivery.models import DeliveryAttempt, MessageState, OutboundMessage
from apps.delivery.outbox import enqueue
from apps.delivery.policy import BusinessIntent
from apps.delivery.timeline import (
    PERIOD_MESSAGE_LIMIT,
    ObservationEffect,
    period_communication,
)
from apps.obligations.coverage import coverage_for
from apps.obligations.generator import generate_das_calendar
from apps.obligations.models import Document, Obligation
from apps.tenants.models import Membership, TenantRole
from tests.cycles.cycle_fixtures import PERIOD, cycle_setup
from tests.cycles.observation_fixtures import payment_claim
from tests.cycles.test_exceptions import _open
from tests.cycles.test_workspace_sections_render import period_world
from tests.portal.request_fixtures import portal_setup
from tests.ui.factories import add_member

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from django.db.models import Model
    from pytest_django.fixtures import SettingsWrapper

    from tests.cycles.cycle_fixtures import CycleSetup
    from tests.cycles.test_workspace_sections_render import PeriodWorld

pytestmark = pytest.mark.django_db(transaction=True)
MOMENT = datetime(2026, 9, 15, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("scope", "status"),
    [("owning", 200), ("unassigned", 404), ("foreign", 404)],
)
def test_workspace_owning_positive_and_scoped_denials(
    settings: SettingsWrapper,
    scope: Literal["owning", "unassigned", "foreign"],
    status: int,
) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost", "testserver"]
    setup = portal_setup(f"t35-scope-{scope}")
    match scope:
        case "owning":
            actor, client_id = setup.graph.actor, setup.graph.client.pk
        case "unassigned":
            actor, client_id = setup.graph.staff, setup.graph.client.pk
        case "foreign":
            foreign = cycle_setup("t35-foreign")
            actor, client_id = setup.graph.actor, foreign.client.pk
        case _:
            assert_never(scope)

    response = setup.firm(actor).get(f"/clientes/{client_id}/?periodo=2026-09")

    assert response.status_code == status
    if status == 200:
        assert response.context["client"].pk == setup.graph.client.pk
        assert response.context["period"] == PERIOD
        assert {row.pk for row in response.context["cycles"]} == {setup.cycle.pk}
        assert {row.request_id for row in response.context["period_requests"]} == {
            setup.request.pk
        }
    else:
        assert str(setup.request.pk).encode() not in response.content


def _unassigned_read(
    reader: Literal["workspace", "cycle_context", "coverage", "timeline"],
    setup: CycleSetup,
) -> None:
    match reader:
        case "workspace":
            read_workspace(setup.staff, setup.client.pk, PERIOD)
        case "cycle_context":
            get_cycle_context(setup.staff, setup.client.pk, PERIOD)
        case "coverage":
            coverage_for(setup.staff, setup.client, PERIOD)
        case "timeline":
            timeline(setup.staff, setup.client.pk, PERIOD)
        case _:
            assert_never(reader)


@pytest.mark.parametrize(
    "reader", ["workspace", "cycle_context", "coverage", "timeline"]
)
def test_direct_readers_still_deny_an_unassigned_actor(
    reader: Literal["workspace", "cycle_context", "coverage", "timeline"],
) -> None:
    setup = cycle_setup(f"t35-direct-{reader}")

    with tenant_context(setup.tenant.pk), pytest.raises(Http404):
        _unassigned_read(reader, setup)


def test_workspace_does_not_retain_a_revoked_assignment() -> None:
    setup = cycle_setup("t35-revoke")
    with tenant_context(setup.tenant.pk):
        assignment = ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        assert (
            read_workspace(setup.staff, setup.client.pk, PERIOD).client.pk
            == setup.client.pk
        )
        assignment.delete()

        with pytest.raises(Http404):
            read_workspace(setup.staff, setup.client.pk, PERIOD)


@pytest.mark.parametrize("grant_state", ["live", "expired", "revoked"])
def test_batched_roster_matches_individual_portfolios_and_live_grants(
    monkeypatch: pytest.MonkeyPatch, grant_state: str
) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: MOMENT)
    setup = cycle_setup(f"t35-roster-{grant_state}")
    operator = add_member(
        setup.tenant, f"operator@{setup.tenant.slug}.test", TenantRole.OPERATIONS_ADMIN
    )
    elevated_staff = add_member(
        setup.tenant,
        f"super-staff@{setup.tenant.slug}.test",
        TenantRole.STAFF_ACCOUNTANT,
    )
    elevated_staff.is_superuser = True
    elevated_staff.save(update_fields=["is_superuser"])
    RoleGrant.objects.filter(
        role=TenantRole.OPERATIONS_ADMIN, capability__slug=VIEW_ALL
    ).update(level=GrantLevel.LIMITED)
    with tenant_context(setup.tenant.pk):
        UserCapabilityGrant.objects.create(
            tenant=setup.tenant,
            user=operator,
            capability=Capability.objects.get(slug=VIEW_ALL),
            level=GrantLevel.FULL,
            expires_at=MOMENT
            if grant_state == "expired"
            else MOMENT + timedelta(days=1),
            revoked_at=MOMENT if grant_state == "revoked" else None,
        )
        ClientAssignment.objects.create(
            tenant=setup.tenant, client=setup.client, user=setup.staff
        )
        assigned = set(
            ClientAssignment.objects.filter(client=setup.client).values_list(
                "user_id", flat=True
            )
        )
        # Independent oracle: the old per-member expression, before measurement.
        members = Membership.objects.filter(
            tenant=setup.tenant, is_active=True, client__isnull=True
        ).select_related("user")
        expected = {
            member.user_id
            for member in members
            if member.user_id in assigned
            or portfolio_scope(member.user, tenant_id=setup.tenant.pk)
            is PortfolioScope.ALL
        }

        with CaptureQueriesContext(connection) as queries:
            actual = list(
                assignable_members(setup.client.pk, tenant_id=setup.tenant.pk)
            )

    assert len(queries) == 1
    assert {member.user_id for member in actual} == expected
    assert setup.actor.pk in expected
    assert setup.staff.pk in expected
    assert elevated_staff.pk not in expected
    assert (operator.pk in expected) == (grant_state == "live")


def _populate_registry(world: PeriodWorld) -> None:
    """Populate every core section and adversarial half-open boundaries."""
    graph = world.setup.graph
    early = Engagement.objects.create(
        id=UUID(int=1),
        tenant=graph.tenant,
        client=graph.client,
        service_kind="mei_mensal",
        starts_on=PERIOD - timedelta(days=1),
        responsible=graph.staff,
        provenance="staff_entry",
    )
    ServiceCycle.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        engagement=early,
        playbook_id=world.setup.cycle.playbook_id,
        playbook_version_id=world.setup.cycle.playbook_version_id,
        period_start=PERIOD,
        period_end=world.setup.cycle.period_end,
        state=CycleState.WAITING,
        owner=graph.staff,
        created_at=world.setup.cycle.created_at + timedelta(seconds=1),
    )
    Engagement.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        service_kind="outro",
        starts_on=PERIOD - timedelta(days=2),
        provenance="staff_entry",
    )
    Engagement.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        service_kind="outro",
        starts_on=PERIOD - timedelta(days=30),
        ends_on=PERIOD,
        provenance="staff_entry",
    )
    ClientContact.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        name="Inactive fixture",
        email="inactive@t35.test",
        active=False,
    )
    OnboardingItem.objects.create(
        tenant=graph.tenant,
        client=graph.client,
        key="t35-blocked",
        label="T35 fixture",
        position=2,
        status="blocked",
        blocked_reason="fixture",
    )
    Document.objects.bulk_create(
        [
            Document(
                tenant=graph.tenant,
                client=graph.client,
                storage_key=f"t35-fixture/{index}",
                sha256=f"{index:064x}",
                original_filename=f"fixture-{index}.pdf",
                content_type="application/pdf",
                byte_size=1,
                uploaded_by=graph.actor,
                client_upload=False,
                created_at=MOMENT + timedelta(seconds=index),
            )
            for index in range(21)
        ]
    )
    generate_das_calendar(graph.client, starting_from=PERIOD)
    subject = Obligation.objects.get(
        client=graph.client, competence_month=PERIOD, obligation_type_id="DAS"
    )
    payment_claim(subject, observed_at=MOMENT)
    payment_claim(
        subject,
        source="official_api",
        verification="refresh_failed",
        outcome="refresh_failed",
        observed_at=MOMENT + timedelta(seconds=1),
    )
    world.setup.cycle.refresh_from_db()
    create_request(
        graph.actor,
        world.setup.cycle.pk,
        RequestFields("T35 receipt", kind="comprovante", obligation_id=subject.pk),
        expected_version=world.setup.cycle.version,
    )
    record_event(
        action=AuditAction.CLIENT_UPDATED,
        tenant_id=graph.tenant.pk,
        actor=graph.actor,
        obj=ObjectRef(type="clients.ClientCompany", id=str(graph.client.pk)),
        metadata={"client_id": str(graph.client.pk)},
    )


def _assert_registry_equivalence(workspace: WorkspaceRead) -> None:
    client, period = workspace.client, workspace.period
    previous_cycles = list(
        ServiceCycle.objects.filter(client=client, period_start=period)
        .select_related("playbook", "owner", "engagement")
        .order_by("created_at", "pk")
    )
    previous_engagements = list(
        Engagement.objects.filter(client=client, starts_on__lte=period)
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gt=period))
        .select_related("responsible")
        .order_by("pk")
    )
    previous_definitions = list(
        PlaybookVersion.objects.filter(state=VersionState.PUBLISHED)
        .select_related("playbook")
        .order_by("playbook_id", "-number")
        .distinct("playbook_id")
    )
    previous_obligations = list(
        Obligation.objects.filter(client=client, competence_month=period)
        .select_related("obligation_type")
        .order_by("competence_month", "pk")
    )
    pairs: tuple[tuple[Iterable[Model], Iterable[Model]], ...] = (
        (workspace.cycles, previous_cycles),
        (workspace.engagements, previous_engagements),
        (workspace.definitions, previous_definitions),
        (workspace.obligations, previous_obligations),
        (
            workspace.documents,
            list(
                Document.objects.filter(client=client).order_by("-created_at", "pk")[
                    :20
                ]
            ),
        ),
        (
            workspace.contacts,
            list(
                ClientContact.objects.filter(client=client, active=True).order_by("pk")
            ),
        ),
        (
            workspace.checklist,
            list(
                OnboardingItem.objects.filter(client=client).order_by("position", "pk")
            ),
        ),
    )
    # The oracle serializes ALL real scalar fields, not the production field lists.
    # Missing projected fields therefore cannot also disappear from the oracle.
    for actual, previous in pairs:
        assert serialize("python", actual) == serialize("python", previous)
    assert [row.playbook.name for row in workspace.cycles] == [
        row.playbook.name for row in previous_cycles
    ]
    assert [
        (row.responsible_id, row.responsible.email if row.responsible else None)
        for row in workspace.engagements
    ] == [
        (row.responsible_id, row.responsible.email if row.responsible else None)
        for row in previous_engagements
    ]
    assert [row.playbook.name for row in workspace.definitions] == [
        row.playbook.name for row in previous_definitions
    ]
    assert [row.obligation_type.name for row in workspace.obligations] == [
        row.obligation_type.name for row in previous_obligations
    ]


def test_populated_projection_equals_the_previous_module_reads(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dispatch and lease cutoffs read the model's unpatched field defaults:
    # freezing the clock before the fixture starves the real claim batch
    # (T36's probe showed due_at_frozen_now=False -> zero claims). Freeze only
    # for the comparison the pinned clock serves (coverage at MOMENT).
    world = period_world(settings, tmp_path, "t35-equivalence")
    monkeypatch.setattr("django.utils.timezone.now", lambda: MOMENT)
    graph = world.setup.graph
    with tenant_context(graph.tenant.pk):
        _populate_registry(world)

        workspace = read_workspace(graph.actor, graph.client.pk, PERIOD)

        _assert_registry_equivalence(workspace)
        previous_context = get_cycle_context(graph.actor, graph.client.pk, PERIOD)
        assert workspace.context == previous_context
        assert workspace.context.cycle_id != workspace.cycles[0].pk
        assert workspace.context.tax_profile is not None
        assert previous_context.tax_profile is not None
        assert serialize("python", [workspace.context.tax_profile]) == serialize(
            "python", [previous_context.tax_profile]
        )
        assert workspace.requests == period_requests(graph.client, PERIOD)
        assert workspace.communication == period_communication(graph.client.pk, PERIOD)
        assert workspace.timeline == timeline(graph.actor, graph.client.pk, PERIOD)
        previous_coverage = coverage_for(graph.actor, graph.client, PERIOD, now=MOMENT)
        assert workspace.coverage == previous_coverage
        for actual, previous in zip(workspace.coverage, previous_coverage, strict=True):
            assert serialize("python", actual.covered_by.values()) == serialize(
                "python", previous.covered_by.values()
            )
        assert len(workspace.documents) == 20
        assert len(workspace.engagements) == 3


def test_selected_terminal_exception_matches_the_standalone_projection(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t35-selected")
    graph = world.setup.graph
    with tenant_context(graph.tenant.pk):
        problem = _open(graph, world.setup.cycle, REQUEST_OVERDUE, world.request.pk)
        problem = transition_exception(
            graph.actor,
            problem.pk,
            ExceptionState.HANDLING,
            expected_version=problem.version,
        )
        problem = transition_exception(
            graph.actor,
            problem.pk,
            ExceptionState.RESOLVED,
            expected_version=problem.version,
            note="Fixture resolution",
        )
        request = AuthenticatedRequest()
        request.user = graph.actor
        rows = CycleException.objects.filter(
            client=graph.client, cycle__period_start=PERIOD
        )
        selection = ExceptionSelection(problem.pk, "t35-conflict")
        previous = exception_rows(
            request, rows, "cliente", selected_exception=problem, error=selection.error
        )
        workspace = read_workspace(graph.actor, graph.client.pk, PERIOD)

        projected = present_exceptions(
            read_exceptions(rows, selection),
            selection,
            workspace.exception_controls,
            "cliente",
        )

    assert projected == previous
    assert [row["selected"] for row in projected] == [True]
    assert [row["is_active"] for row in projected] == [False]


@pytest.mark.parametrize("profile_case", ["gap", "superseded"])
def test_effective_profile_and_empty_period_keep_standalone_answers(
    profile_case: str,
) -> None:
    setup = cycle_setup(f"t35-profile-{profile_case}")
    with tenant_context(setup.tenant.pk):
        profile = TaxProfile.objects.get(client=setup.client)
        if profile_case == "gap":
            profile.valid_from = PERIOD + timedelta(days=31)
            profile.save(update_fields=["valid_from"])
        else:
            profile.superseded_at = MOMENT
            profile.save(update_fields=["superseded_at"])
            TaxProfile.objects.create(
                tenant=setup.tenant,
                client=setup.client,
                regime="simples",
                valid_from=PERIOD,
                provenance="staff_entry",
            )

        workspace = read_workspace(setup.actor, setup.client.pk, PERIOD)

        assert workspace.context == get_cycle_context(
            setup.actor, setup.client.pk, PERIOD
        )
        assert workspace.coverage == coverage_for(setup.actor, setup.client, PERIOD)
        assert workspace.cycles == []
        assert workspace.requests == []
        assert workspace.communication == period_communication(setup.client.pk, PERIOD)
        assert workspace.timeline == timeline(setup.actor, setup.client.pk, PERIOD)


def _fill_message_page(world: PeriodWorld) -> list[OutboundMessage]:
    graph, cycle = world.setup.graph, world.setup.cycle
    rows = []
    for index in range(PERIOD_MESSAGE_LIMIT):
        graph.client.refresh_from_db()
        row = enqueue(
            graph.actor,
            graph.client.pk,
            world.setup.contact.pk,
            intent=BusinessIntent(
                graph.engagement.pk,
                cycle.playbook_id,
                cycle.period_start,
                cycle.period_end,
                cycle.amendment,
                "notice",
                "email_notice",
                index + 1,
            ),
            kind="notice",
            expected_version=graph.client.version,
            cycle_id=cycle.pk,
        )
        row.created_at = world.publication_notice.created_at + timedelta(
            seconds=index + 1
        )
        row.save(update_fields=["created_at"])
        rows.append(row)
    return rows


def test_message_and_receipt_windows_remain_distinct_and_complete(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    world = period_world(settings, tmp_path, "t35-windows")
    graph = world.setup.graph
    with tenant_context(graph.tenant.pk):
        messages = _fill_message_page(world)
        newest = max(
            entry.when for entry in timeline(graph.actor, graph.client.pk, PERIOD)
        )
        receipt_start = newest + timedelta(seconds=1)
        receipts = DeliveryAttempt.objects.bulk_create(
            [
                DeliveryAttempt(
                    tenant=graph.tenant,
                    client=graph.client,
                    message=messages[-1],
                    outcome=MessageState.CANCELLED,
                    error_code="stale_fence",
                    occurred_at=receipt_start + timedelta(seconds=index),
                )
                for index in range(105)
            ]
        )
        older_receipt = DeliveryAttempt.objects.create(
            tenant=graph.tenant,
            client=graph.client,
            message=world.request_notice,
            outcome=MessageState.CANCELLED,
            error_code="late_observation",
            occurred_at=receipt_start + timedelta(seconds=106),
        )

        workspace = read_workspace(graph.actor, graph.client.pk, PERIOD)

        assert workspace.communication == period_communication(graph.client.pk, PERIOD)
        assert workspace.timeline == timeline(graph.actor, graph.client.pk, PERIOD)
    shown = {row.message.pk: row for row in workspace.communication.messages}
    assert len(shown) == PERIOD_MESSAGE_LIMIT
    assert workspace.communication.more_messages
    assert world.request_notice.pk not in shown
    observations = shown[messages[-1].pk].observations
    # The enqueue journal receipt precedes every delivery observation (T44).
    assert observations[0].code == "enqueued"
    assert observations[0].effect is ObservationEffect.APPLIED
    assert len(observations) == len(receipts) + 1 == 106
    assert {observation.effect for observation in observations} == {
        ObservationEffect.APPLIED,
        ObservationEffect.IGNORED_STALE,
    }
    assert {observation.effect for observation in observations[1:]} == {
        ObservationEffect.IGNORED_STALE
    }
    assert older_receipt.pk in {entry.id for entry in workspace.timeline}
    assert len(workspace.timeline) == 100
    assert receipts[0].pk not in {entry.id for entry in workspace.timeline}
