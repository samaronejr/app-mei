"""Firm-side request commands: lock, version, can() inside the lock, event, portfolio.

Pitfalls the review names, each proven here: a stale second caller gets a typed
conflict with NO writes; an unassigned staff accountant gets 404 with no rows written;
a replayed old form cannot undo a newer change; every command writes one event.
"""

import threading
from http import HTTPStatus

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404

from apps.core.tenancy import tenant_context
from apps.cycles.errors import IllegalTransition, VersionConflict
from apps.cycles.models import (
    CycleEvent,
    EvidenceRequest,
    RequestInternal,
    RequestState,
)
from apps.cycles.services.requests import (
    RequestFields,
    create_request,
    remind_request,
    update_request,
    waive_request,
)
from apps.delivery.models import OutboundMessage
from apps.tenants.models import TenantRole
from tests.portal.request_fixtures import PortalSetup, portal_setup
from tests.support_threads import run_in_thread_as_runtime

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def setup() -> PortalSetup:
    return portal_setup("req-services")


def _rows(setup: PortalSetup) -> tuple[int, int, int, int]:
    with tenant_context(setup.graph.tenant.pk):
        return (
            EvidenceRequest.objects.count(),
            RequestInternal.objects.count(),
            CycleEvent.objects.count(),
            OutboundMessage.objects.count(),
        )


def test_create_writes_request_internal_and_one_event(setup: PortalSetup) -> None:
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        before_events = CycleEvent.objects.count()
        request = create_request(
            setup.graph.actor,
            setup.cycle.pk,
            RequestFields(title="  Extrato  ", instructions="só para a equipe"),
            expected_version=setup.cycle.version,
        )
        assert request.title == "Extrato"
        assert request.state == RequestState.DRAFT
        assert RequestInternal.objects.get(request=request).instructions == (
            "só para a equipe"
        )
        event = CycleEvent.objects.exclude(pk__in=[]).latest("occurred_at", "pk")
        assert CycleEvent.objects.count() == before_events + 1
        assert (event.type, event.aggregate_id) == ("request.created", request.pk)
        # Titles and instructions never enter the append-only payload.
        assert "Extrato" not in str(event.payload)


def test_two_callers_with_one_version_one_wins_and_the_loser_writes_nothing(
    setup: PortalSetup,
) -> None:
    version = setup.request.version
    fields = RequestFields(title="Primeira edição")
    with tenant_context(setup.graph.tenant.pk):
        update_request(
            setup.graph.actor, setup.request.pk, fields, expected_version=version
        )
    before = _rows(setup)
    stale = RequestFields(title="Edição atrasada")

    def second_caller() -> None:
        update_request(
            setup.graph.actor, setup.request.pk, stale, expected_version=version
        )

    with pytest.raises(VersionConflict):
        run_in_thread_as_runtime(setup.graph.tenant.pk, second_caller, timeout=20)
    assert _rows(setup) == before
    stored = setup.reload(setup.request)
    assert stored.title == "Primeira edição"
    assert stored.version == version + 1


def test_concurrent_waive_and_edit_serialize_on_the_row_lock(
    setup: PortalSetup,
) -> None:
    """Both callers read version N; the lock makes exactly one of them win."""
    version = setup.request.version
    barrier = threading.Barrier(2, timeout=20)
    outcomes: list[str] = []

    def waive() -> None:
        barrier.wait()
        try:
            waive_request(
                setup.graph.actor,
                setup.request.pk,
                reason="sem movimento",
                expected_version=version,
            )
            outcomes.append("waived")
        except VersionConflict:
            outcomes.append("conflict")

    def edit() -> None:
        barrier.wait()
        try:
            update_request(
                setup.graph.actor,
                setup.request.pk,
                RequestFields(title="Edição concorrente"),
                expected_version=version,
            )
            outcomes.append("edited")
        except (VersionConflict, IllegalTransition):
            outcomes.append("conflict")

    threads = [
        threading.Thread(
            target=run_in_thread_as_runtime,
            args=(setup.graph.tenant.pk, work),
            kwargs={"timeout": 20},
        )
        for work in (waive, edit)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes).count("conflict") == 1, outcomes
    assert setup.reload(setup.request).version == version + 1


def test_unassigned_staff_accountant_gets_404_and_nothing_is_written(
    setup: PortalSetup,
) -> None:
    staff = setup.graph.staff
    before = _rows(setup)
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        for call in (
            lambda: create_request(
                staff,
                setup.cycle.pk,
                RequestFields(title="x"),
                expected_version=setup.cycle.version,
            ),
            lambda: update_request(
                staff,
                setup.request.pk,
                RequestFields(title="x"),
                expected_version=setup.request.version,
            ),
            lambda: waive_request(
                staff,
                setup.request.pk,
                reason="x",
                expected_version=setup.request.version,
            ),
            lambda: remind_request(
                staff, setup.request.pk, expected_version=setup.request.version
            ),
        ):
            with pytest.raises(Http404):
                call()
    assert _rows(setup) == before
    response = setup.firm(staff).post(
        f"/ciclos/{setup.cycle.pk}/solicitacoes/",
        {
            "action": "waive",
            "request_id": str(setup.request.pk),
            "expected_version": setup.request.version,
            "reason": "x",
        },
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert _rows(setup) == before


def test_can_is_rechecked_inside_the_lock(setup: PortalSetup) -> None:
    """A capability withdrawn after the page rendered refuses with nothing written."""
    from apps.authz.models import GrantLevel, RoleGrant  # noqa: PLC0415

    RoleGrant.objects.filter(
        role=TenantRole.OWNER, capability__slug="cycles.operate"
    ).update(level=GrantLevel.NONE)
    before = _rows(setup)
    with tenant_context(setup.graph.tenant.pk), pytest.raises(PermissionDenied):
        waive_request(
            setup.graph.actor,
            setup.request.pk,
            reason="x",
            expected_version=setup.request.version,
        )
    assert _rows(setup) == before
    assert setup.reload(setup.request).state == RequestState.REQUESTED


def test_replaying_an_old_form_does_not_undo_a_newer_change(
    setup: PortalSetup,
) -> None:
    firm = setup.firm()
    old = setup.request.version
    payload = {
        "action": "update",
        "request_id": str(setup.request.pk),
        "expected_version": old,
        "title": "Versão nova",
    }
    assert firm.post(
        f"/ciclos/{setup.cycle.pk}/solicitacoes/", payload
    ).status_code == (HTTPStatus.FOUND)
    replay = firm.post(
        f"/ciclos/{setup.cycle.pk}/solicitacoes/", {**payload, "title": "Antiga"}
    )
    assert replay.status_code == HTTPStatus.CONFLICT
    assert setup.reload(setup.request).title == "Versão nova"


def test_remind_refuses_an_answered_request(setup: PortalSetup) -> None:
    with tenant_context(setup.graph.tenant.pk):
        waive_request(
            setup.graph.actor,
            setup.request.pk,
            reason="x",
            expected_version=setup.request.version,
        )
        current = setup.reload(setup.request)
        with pytest.raises(IllegalTransition):
            remind_request(
                setup.graph.actor, current.pk, expected_version=current.version
            )
