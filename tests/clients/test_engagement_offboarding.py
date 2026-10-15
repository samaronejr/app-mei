"""Engagement expiry removes access without erasing the firm's evidence."""

from datetime import date
from http import HTTPStatus
from pathlib import Path

import pytest
from pytest_django.fixtures import SettingsWrapper

from apps.clients.maintenance import EngagementData, save_engagement
from apps.clients.models import Engagement, ServiceKind
from apps.clients.offboarding import offboard_engagements
from apps.core.tenancy import tenant_context
from apps.cycles.models import CycleEvent, DocumentIntake, EvidenceLink, ServiceCycle
from apps.obligations.models import Document
from apps.tenants.models import Membership
from tests.portal.request_fixtures import PortalSetup, portal_setup, upload

pytestmark = pytest.mark.django_db(transaction=True)

ENDS_ON = date(2026, 9, 30)
BEFORE_GRACE = date(2026, 10, 29)
AFTER_GRACE = date(2026, 10, 30)
ORIGINAL = b"%PDF-1.7\nretained offboarding evidence\n%%EOF"


@pytest.fixture(autouse=True)
def _request_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.clients.offboarding.localdate", lambda: date(2026, 10, 1))


def ended_engagement(slug: str) -> PortalSetup:
    setup = portal_setup(slug)
    tenant = setup.graph.tenant
    tenant.features = [*tenant.features, "firm_settings_r1"]
    tenant.save(update_fields=["features"])
    with tenant_context(tenant.pk):
        engagement = setup.graph.engagement
        save_engagement(
            actor=setup.graph.actor,
            tenant_id=tenant.pk,
            client_id=setup.graph.client.pk,
            engagement_id=engagement.pk,
            expected_version=engagement.version,
            data=EngagementData(
                engagement.service_kind,
                engagement.starts_on,
                ends_on=ENDS_ON,
                responsible_id=setup.graph.actor.pk,
            ),
        )
    return setup


def test_grace_boundary_revokes_portal_and_preserves_evidence(
    settings: SettingsWrapper, tmp_path: Path
) -> None:
    settings.MEDIA_ROOT = tmp_path
    setup = ended_engagement("offboarding-retained")
    portal = setup.portal()
    uploaded = portal.post(
        f"/solicitacoes/{setup.request.pk}/enviar",
        {"file": upload("prova.pdf", ORIGINAL)},
    )
    assert uploaded.status_code == HTTPStatus.FOUND
    with tenant_context(setup.graph.tenant.pk):
        document = Document.objects.get()
        digest, document_key = document.sha256, document.storage_key
        key = DocumentIntake.objects.get(document=document).quarantine_key
        unrelated_state = ServiceCycle.objects.get(
            pk=setup.other_request.cycle_id
        ).state

    offboard_engagements(setup.graph.tenant.pk, on=BEFORE_GRACE)
    assert portal.get("/").status_code == HTTPStatus.OK
    offboard_engagements(setup.graph.tenant.pk, on=AFTER_GRACE)
    assert portal.get("/").status_code == HTTPStatus.FORBIDDEN
    with tenant_context(setup.graph.tenant.pk):
        assert not Membership.objects.filter(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            user=setup.portal_user,
            is_active=True,
        ).exists()
        setup.cycle.refresh_from_db()
        assert setup.cycle.state == "cancelado"
        assert ServiceCycle.objects.get(pk=setup.other_request.cycle_id).state == (
            unrelated_state
        )
        document.refresh_from_db()
        assert (document.sha256, document.storage_key, document.purged_at) == (
            digest,
            document_key,
            None,
        )
        assert DocumentIntake.objects.get(document=document).quarantine_key == key
        assert EvidenceLink.objects.filter(document=document).count() == 1
    assert (tmp_path / key).read_bytes() == ORIGINAL


def test_another_live_engagement_keeps_the_clients_portal_access() -> None:
    setup = ended_engagement("offboarding-live-sibling")
    with tenant_context(setup.graph.tenant.pk):
        Engagement.objects.create(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            service_kind=ServiceKind.DOCUMENTOS_MENSAL,
            starts_on=date(2026, 9, 1),
            provenance="staff_entry",
        )
    offboard_engagements(setup.graph.tenant.pk, on=AFTER_GRACE)
    assert setup.portal().get("/").status_code == HTTPStatus.OK
    with tenant_context(setup.graph.tenant.pk):
        setup.cycle.refresh_from_db()
        assert setup.cycle.state == "cancelado"
        assert Membership.objects.filter(
            tenant=setup.graph.tenant,
            client=setup.graph.client,
            user=setup.portal_user,
            is_active=True,
        ).exists()


def test_a_delayed_sweep_does_not_extend_portal_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = ended_engagement("offboarding-delayed")
    portal = setup.portal()
    monkeypatch.setattr("apps.clients.offboarding.localdate", lambda: AFTER_GRACE)
    assert portal.get("/").status_code == HTTPStatus.FORBIDDEN
    # A GET refuses access without silently performing the scheduled mutation.
    assert Membership.objects.filter(
        tenant=setup.graph.tenant, client=setup.graph.client, is_active=True
    ).exists()


def test_repeated_sweeps_do_not_duplicate_cancellation_events() -> None:
    setup = ended_engagement("offboarding-replay")
    offboard_engagements(setup.graph.tenant.pk, on=AFTER_GRACE)
    with tenant_context(setup.graph.tenant.pk):
        first = list(
            CycleEvent.objects.filter(cycle=setup.cycle)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        assert CycleEvent.objects.filter(
            cycle=setup.cycle, type="cycle.offboarded"
        ).exists()
        version = ServiceCycle.objects.get(pk=setup.cycle.pk).version
    offboard_engagements(setup.graph.tenant.pk, on=AFTER_GRACE)
    with tenant_context(setup.graph.tenant.pk):
        after = list(
            CycleEvent.objects.filter(cycle=setup.cycle)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        assert after == first
        assert ServiceCycle.objects.get(pk=setup.cycle.pk).version == version


def test_an_extension_before_the_sweep_keeps_access_and_cycles() -> None:
    setup = ended_engagement("offboarding-extension")
    with tenant_context(setup.graph.tenant.pk):
        engagement = Engagement.objects.get(pk=setup.graph.engagement.pk)
        save_engagement(
            actor=setup.graph.actor,
            tenant_id=setup.graph.tenant.pk,
            client_id=setup.graph.client.pk,
            engagement_id=engagement.pk,
            expected_version=engagement.version,
            data=EngagementData(
                engagement.service_kind,
                engagement.starts_on,
                ends_on=date(2026, 12, 31),
                responsible_id=setup.graph.actor.pk,
            ),
        )
        previous = ServiceCycle.objects.get(pk=setup.cycle.pk).state
    offboard_engagements(setup.graph.tenant.pk, on=AFTER_GRACE)
    assert setup.portal().get("/").status_code == HTTPStatus.OK
    with tenant_context(setup.graph.tenant.pk):
        assert ServiceCycle.objects.get(pk=setup.cycle.pk).state == previous


def test_unflagged_firms_are_not_offboarded_by_the_new_sweep() -> None:
    setup = ended_engagement("offboarding-unflagged")
    setup.graph.tenant.features = ["cycles_r1"]
    setup.graph.tenant.save(update_fields=["features"])
    offboard_engagements(setup.graph.tenant.pk, on=AFTER_GRACE)
    assert setup.portal().get("/").status_code == HTTPStatus.OK
    with tenant_context(setup.graph.tenant.pk):
        assert ServiceCycle.objects.get(pk=setup.cycle.pk).state != "cancelado"
