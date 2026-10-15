"""Compare the displayed closure cut with the unchanged authoritative checker."""

from datetime import timedelta

import pytest
from django.db.models import F
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.cycles.completion_checks import completion_items, valid_waiver
from apps.cycles.evidence_index import capture_index
from apps.cycles.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    CycleEvent,
    EvidenceRequest,
    InternalNote,
    RequestInternal,
    RequestState,
)
from apps.cycles.services.requests import waive_request
from tests.cycles.display_fixtures import populated_detail

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _hosts(settings: SettingsWrapper) -> None:
    settings.ALLOWED_HOSTS = [".localhost", "localhost"]


@pytest.mark.parametrize("index_state", ["absent", "current", "stale"])
def test_displayed_checklist_and_blockers_equal_authoritative_populated_cycle(
    index_state: str,
) -> None:
    # Given every closure category, including a receipt-backed approved waiver.
    world = populated_detail(f"t36-equivalence-{index_state}")
    with tenant_context(world.graph.tenant.pk):
        if index_state != "absent":
            capture_index(
                world.graph.actor,
                world.cycle.pk,
                expected_version=world.cycle.version,
            )
        if index_state == "stale":
            # No cycle-version shortcut can notice a source-only title change.
            EvidenceRequest.objects.filter(pk=world.request.pk).update(
                title="Changed source title after capture"
            )
        world.cycle.refresh_from_db()
        expected = completion_items(world.cycle)
        codes = {row.code for row in expected}
        assert {"tasks", "requests", "exceptions", "actions"} <= codes
        assert ("evidence_index" in codes) is (index_state != "current")
        waived = EvidenceRequest.objects.get(
            cycle=world.cycle, state=RequestState.WAIVED
        )
        assert valid_waiver(waived)
        assert f"requests:{waived.pk}:{waived.version}" not in {
            row.key for row in expected
        }
        frozen = [
            (row.pk, bytes(row.manifest_bytes), row.digest, row.input_spec, row.version)
            for row in ActionProposal.objects.order_by("pk")
        ]
        before = (
            CycleEvent.objects.count(),
            Approval.objects.count(),
            ActionExecution.objects.count(),
        )
        notes = set(
            InternalNote.objects.filter(cycle=world.cycle).values_list("pk", flat=True)
        )
    # When the real GET renders the populated graph.
    response = world.firm().get(f"/ciclos/{world.cycle.pk}/")
    # Then keys, versions, category order and resolution links all agree.
    assert response.status_code == 200
    assert response.context["completion_items"] == expected
    assert {row.pk for row in response.context["proposals"]} == {
        row[0] for row in frozen
    }
    assert notes
    assert {row.pk for row in response.context["notes"]} == notes
    assert {
        row["href"]: row["passed"] for row in response.context["completion_checklist"]
    } == {
        "#graph-title": "tasks" not in codes,
        "#solicitacoes": "requests" not in codes,
        "#pendencias": "exceptions" not in codes,
        "#cycle-proposals": "actions" not in codes,
        f"/ciclos/{world.cycle.pk}/evidencias/": "evidence_index" not in codes,
    }
    assert response.context["cycle"].version == world.cycle.version
    assert (
        f'name="expected_version" value="{world.cycle.version}"'.encode()
        in response.content
    )
    with tenant_context(world.graph.tenant.pk):
        assert [
            (row.pk, bytes(row.manifest_bytes), row.digest, row.input_spec, row.version)
            for row in ActionProposal.objects.order_by("pk")
        ] == frozen
        assert (
            CycleEvent.objects.count(),
            Approval.objects.count(),
            ActionExecution.objects.count(),
        ) == before


@pytest.mark.parametrize(
    "control", ["exact", "reason", "revision", "receipt", "internal", "expired_now"]
)
def test_displayed_waiver_validity_equals_authority_with_negative_controls(
    monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    # Given a valid waiver, or one exact predicate deliberately made false.
    world = populated_detail(f"t36-waiver-{control.replace('_', '-')}")
    with tenant_context(world.graph.tenant.pk):
        waived = EvidenceRequest.objects.get(
            cycle=world.cycle, state=RequestState.WAIVED
        )
        assert valid_waiver(waived)
        if control == "reason":
            RequestInternal.objects.filter(request=waived).update(
                waiver_reason="A different reason from the approved bytes."
            )
        elif control == "revision":
            EvidenceRequest.objects.filter(pk=waived.pk).update(
                version=F("version") + 1
            )
        elif control == "receipt":
            waived = waive_request(
                world.graph.actor,
                world.request.pk,
                reason="Legacy waiver with no executed approval.",
                expected_version=world.request.version,
            )
        elif control == "internal":
            waived = EvidenceRequest.objects.create(
                tenant=world.graph.tenant,
                client=world.graph.client,
                cycle=world.cycle,
                created_by=world.graph.actor,
                title="Waived row without its private companion",
                state=RequestState.WAIVED,
                version=2,
            )
        elif control == "expired_now":
            later = timezone.now() + timedelta(days=30)
            assert Approval.objects.filter(expires_at__gte=later).count() == 0
            monkeypatch.setattr(timezone, "now", lambda: later)
        waived.refresh_from_db()
        validity = valid_waiver(waived)
        assert validity is (control in {"exact", "expired_now"})
        world.cycle.refresh_from_db()
        expected = completion_items(world.cycle)
    # When the same cycle is displayed after the change.
    response = world.firm().get(f"/ciclos/{world.cycle.pk}/")
    # Then the waived item is blocked exactly when the authoritative receipt fails.
    assert response.status_code == 200
    assert response.context["completion_items"] == expected
    key = f"requests:{waived.pk}:{waived.version}"
    assert (key in {row.key for row in response.context["completion_items"]}) is (
        not validity
    )
