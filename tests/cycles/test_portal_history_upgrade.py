"""A populated legacy upgrade must close former, not just current, privacy."""

from __future__ import annotations

from typing import cast

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from apps.clients.maintenance import EngagementData, save_engagement
from apps.core.tenancy import tenant_context
from apps.cycles.effects import executors
from apps.cycles.evidence import EvidenceLink, EvidenceRequest, RequestState
from apps.cycles.models import ActionExecution, ActionProposal, CycleEvent, CycleState
from apps.cycles.services.cycle import transition_cycle
from apps.cycles.services.requests import RequestFields, create_request, send_requests
from apps.integrations.serpro.pgmei import obtain_das_guide
from apps.obligations.models import SourceObservation
from tests.cycles.portal_upgrade_fixtures import (
    PORTAL_HISTORY,
    PORTAL_LIFETIME,
    PORTAL_READ,
    attach_visible_file,
    boundary_nodes,
    complete_guide_cycle,
    legacy_waiver_write,
    migration_owner,
    omit_revision_two,
    pre_portal_database,
    repair_reason,
)
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    GUIDE_PDF,
    GuideWorld,
    Portal,
    PublishedGuide,
    approved_guide_waiver,
    das_reply,
    guide_world,
    portal_member,
    published_guide,
    session,
)
from tests.portal.request_fixtures import scoped_portal_cursor
from tests.support import enrol_totp

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["approved_guide_waiver", "guide_world", "published_guide"]


@pytest.fixture
def ready(guide_world: GuideWorld) -> tuple[GuideWorld, Portal]:
    enrol_totp(guide_world.owner)
    enrol_totp(guide_world.staff)
    return guide_world, portal_member(guide_world)


@pytest.mark.parametrize("deployed", [0, 1], ids=["fresh", "lookup-only-0025"])
def test_populated_upgrade_preserves_private_history_and_ordinary_control(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, deployed: int
) -> None:
    def approved_download(_boundary: str) -> None:
        response = session(world, guide.portal.user, portal=True).get(guide.url)
        assert (response.status_code, response.content) == (200, GUIDE_PDF)

    with pre_portal_database(
        deployed=deployed, lookup_only=True, inspect_boundary=approved_download
    ):
        # Loading fixtures here ensures issuance, review, publication and
        # independent waiver approval all happen before the portal migration.
        guide = cast("PublishedGuide", request.getfixturevalue("published_guide"))
        waiver = cast(
            "ActionProposal", request.getfixturevalue("approved_guide_waiver")
        )
        world = guide.world
        with tenant_context(world.tenant_id):
            with monkeypatch.context() as legacy:
                legacy.setattr(executors, "waive_request", legacy_waiver_write)
                assert executors.execute(
                    world.owner, waiver.pk, expected_version=waiver.version
                ).executed
            former = EvidenceRequest.objects.get(pk=guide.link.request_id)
            assert former.state == RequestState.WAIVED
            assert ActionExecution.objects.filter(proposal=waiver).exists()
            world.cycle.refresh_from_db()
            ordinary = create_request(
                world.owner,
                world.cycle.pk,
                RequestFields(former.title),
                expected_version=world.cycle.version,
            )
            world.cycle.refresh_from_db()
            send_requests(
                world.owner, world.cycle.pk, expected_version=world.cycle.version
            )
            ordinary.refresh_from_db()
            ordinary_link = attach_visible_file(world, ordinary)
            # This second row has no request-history events. A legacy direct
            # writer changed its state without changing its revision.
            ambiguous = EvidenceRequest.objects.create(
                tenant_id=world.tenant_id,
                client_id=ordinary.client_id,
                cycle=world.cycle,
                title="Unrelated label",
                state="interno",
                required=False,
                created_by=world.owner,
            )
            ambiguous_link = attach_visible_file(world, ambiguous)
            gap, gap_link = omit_revision_two(world)
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE cycles_evidencerequest SET state=%s WHERE id=%s",
                    [RequestState.REQUESTED, ambiguous.pk],
                )
            history = list(
                CycleEvent.objects.filter(aggregate_id=former.pk)
                .order_by("pk")
                .values_list("pk", "type", "actor_id", "payload")
            )
            assert any(payload.get("from") == "interno" for _, _, _, payload in history)
            ordinary_identity = (
                ordinary.state,
                ordinary.version,
                ordinary.created_by_id,
            )

    _visibility(guide.link, visible=False)
    _visibility(ambiguous_link, visible=False)
    _visibility(gap_link, visible=False)
    _visibility(ordinary_link, visible=True)
    assert repair_reason(world, ambiguous) == "missing_creation"
    assert repair_reason(world, gap) == "revision_gap_or_duplicate"
    assert repair_reason(world, ordinary) is None
    # Downgrade must fail before touching either the data or its protections.
    executor = MigrationExecutor(connection)
    nodes = boundary_nodes(executor)
    state = executor.loader.project_state([nodes[-2]])
    with migration_owner(), pytest.raises(IrreversibleError):
        executor.unapply_migration(state, executor.loader.get_migration(*nodes[-1]))
    _visibility(guide.link, visible=False)
    with tenant_context(world.tenant_id):
        former.refresh_from_db()
        ordinary.refresh_from_db()
        assert (former.state, former.created_by_id) == ("interno", world.owner.pk)
        assert (
            ordinary.state,
            ordinary.version,
            ordinary.created_by_id,
        ) == ordinary_identity
        assert all(
            CycleEvent.objects.filter(
                pk=pk, type=kind, actor_id=actor, payload=payload
            ).exists()
            for pk, kind, actor, payload in history
        )
        assert ActionExecution.objects.filter(proposal=waiver).exists()
    approved_download("after refused downgrade")


@pytest.mark.parametrize("action", ["cancelled", "completed", "reassigned"])
def test_supported_cycle_action_keeps_the_historical_request_private(
    request: pytest.FixtureRequest,
    action: str,
) -> None:
    with pre_portal_database():
        world = cast("GuideWorld", request.getfixturevalue("guide_world"))
        with FakeSerpro((das_reply(),)) as peer:
            issued = obtain_das_guide(
                world.owner, world.tenant_id, world.cycle.pk, origin=peer.origin
            )
        assert issued.link_id is not None
        with tenant_context(world.tenant_id):
            link = EvidenceLink.objects.get(pk=issued.link_id)
            former = link.request
            visible_source = attach_visible_file(world, former)
            cycle = world.cycle
            cycle.refresh_from_db()
            if action == "cancelled":
                transition_cycle(
                    world.owner,
                    cycle.pk,
                    CycleState.CANCELLED,
                    expected_version=cycle.version,
                )
                cycle.refresh_from_db()
                assert cycle.state == CycleState.CANCELLED
            elif action == "completed":
                complete_guide_cycle(world)
            else:
                # Requests have no assignee. Change the engagement's responsible
                # person using the supported versioned staff workflow.
                engagement = world.engagement
                edited = save_engagement(
                    actor=world.owner,
                    tenant_id=world.tenant_id,
                    client_id=cycle.client_id,
                    engagement_id=engagement.pk,
                    expected_version=engagement.version,
                    data=EngagementData(
                        service_kind=engagement.service_kind,
                        starts_on=engagement.starts_on,
                        ends_on=engagement.ends_on,
                        responsible_id=world.staff.pk,
                    ),
                )
                assert edited.responsible_id == world.staff.pk
            former.refresh_from_db()
            assert former.state == RequestState.INTERNAL
    _visibility(visible_source, visible=False)
    with tenant_context(world.tenant_id):
        former.refresh_from_db()
        assert former.state == RequestState.INTERNAL
        assert former.created_by_id == world.owner.pk
        assert EvidenceLink.objects.filter(pk=link.pk, request_id=former.pk).exists()


def test_only_internal_artifacts_override_complete_ordinary_histories(
    request: pytest.FixtureRequest,
) -> None:
    with pre_portal_database():
        guide = cast("PublishedGuide", request.getfixturevalue("published_guide"))
        world = guide.world
        with tenant_context(world.tenant_id):
            rows = []
            for _ in range(3):
                world.cycle.refresh_from_db()
                rows.append(
                    create_request(
                        world.owner,
                        world.cycle.pk,
                        RequestFields("Identical display label"),
                        expected_version=world.cycle.version,
                    )
                )
            world.cycle.refresh_from_db()
            send_requests(
                world.owner, world.cycle.pk, expected_version=world.cycle.version
            )
            by_document, by_fact, staff_fact = rows
            guide_link = EvidenceLink.objects.create(
                tenant_id=world.tenant_id,
                client_id=world.cycle.client_id,
                request=by_document,
                document_id=guide.link.document_id,
                origin="staff",
                submitted_by=world.owner,
            )
            fact_link = attach_visible_file(world, by_fact)
            public_link = attach_visible_file(world, staff_fact)
            for link, source in (
                (fact_link, "official_api"),
                (public_link, "staff_recorded"),
            ):
                SourceObservation.objects.create(
                    tenant_id=world.tenant_id,
                    client_id=world.cycle.client_id,
                    subject_ref=world.obligation,
                    kind="guide_issued",
                    period=world.cycle.period_start,
                    source_kind=source,
                    observed_at=guide.publication.published_at,
                    value_json={"sha256": link.document.sha256},
                    verification_status="verified"
                    if source == "official_api"
                    else "unknown",
                    outcome="accepted" if source == "official_api" else "recorded",
                    recorded_by=None if source == "official_api" else world.staff,
                    evidence_link=link,
                )
    _visibility(guide_link, visible=False)
    _visibility(fact_link, visible=False)
    _visibility(public_link, visible=True)
    assert repair_reason(world, by_document) == "system_das_artifact"
    assert repair_reason(world, by_fact) == "official_guide_observation"
    assert repair_reason(world, staff_fact) is None


@pytest.mark.parametrize(
    ("deployed", "lookup_only"),
    [(0, False), (1, True), (1, False), (2, False)],
    ids=["fresh", "lookup-only-0025", "original-0025", "original-0026"],
)
def test_first_corrected_boundary_hides_before_releasing_its_lock(
    request: pytest.FixtureRequest, deployed: int, lookup_only: bool
) -> None:
    observed: dict[str, tuple[tuple[bool, bool, bool], tuple[str] | None]] = {}

    def inspect_boundary(name: str) -> None:
        access = _read_visibility(link)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tgenabled FROM pg_catalog.pg_trigger "
                "WHERE tgrelid='public.cycles_evidencerequest'::regclass "
                "AND tgname='cycles_keep_internal_request'"
            )
            observed[name] = (access, cursor.fetchone())

    with pre_portal_database(
        deployed=deployed, lookup_only=lookup_only, inspect_boundary=inspect_boundary
    ):
        world = cast("GuideWorld", request.getfixturevalue("guide_world"))
        with tenant_context(world.tenant_id):
            # An exact non-title internal transition, independent of PGMEI artifacts.
            world.cycle.refresh_from_db()
            row = create_request(
                world.owner,
                world.cycle.pk,
                RequestFields("Private record"),
                expected_version=world.cycle.version,
            )
            EvidenceRequest.objects.filter(pk=row.pk).update(
                state=RequestState.INTERNAL
            )
            legacy_waiver_write(
                world.owner, row.pk, reason="Old waiver.", expected_version=row.version
            )
            link = attach_visible_file(world, row)
    for n, (_, node) in enumerate((PORTAL_READ, PORTAL_LIFETIME, PORTAL_HISTORY)):
        access, guard = observed[node]
        expected_visible = n < deployed
        assert access == (expected_visible, expected_visible, True)
        if not expected_visible:
            assert guard == ("O",)
    assert repair_reason(world, row) == "internal_transition"


def _read_visibility(link: EvidenceLink) -> tuple[bool, bool, bool]:
    with scoped_portal_cursor(link.tenant_id, link.client_id) as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM cycles_evidencerequest WHERE id=%s), "
            "EXISTS (SELECT 1 FROM cycles_evidencelink WHERE id=%s), "
            "EXISTS (SELECT 1 FROM obligations_document WHERE id=%s)",
            [link.request_id, link.pk, link.document_id],
        )
        result = cursor.fetchone()
        assert result is not None
        return result[0], result[1], result[2]


def _visibility(link: EvidenceLink, *, visible: bool) -> None:
    assert _read_visibility(link) == (visible, visible, True)
