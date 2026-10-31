"""AXRM-017: the official MEI DAS guide (PGMEI) on staff request, never scheduled."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from django.conf import settings as django_settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.utils import timezone

from apps.audit.models import Event
from apps.clients.models import TaxProfile
from apps.core.tenancy import tenant_context
from apps.cycles.approval_forms import ProposalForm
from apps.cycles.evidence import (
    CLIENT_VISIBLE_STATES,
    DocumentIntake,
    EvidenceLink,
    EvidenceRequest,
    IntakeState,
    LinkOrigin,
)
from apps.cycles.services.requests import send_requests
from apps.integrations.allowlist import SERPRO_SERVICES
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.call_models import CallState, ProviderCall
from apps.obligations.models import AcquisitionOrigin, Document, SourceObservation
from apps.tenants.models import Tenant
from tests.fakes.provider import Reply
from tests.fakes.serpro import FakeSerpro
from tests.integrations.pgmei_support import (
    COMPETENCE,
    GUIDE_PDF,
    PRICE_MICROBRL,
    GuideWorld,
    LazyModule,
    das_reply,
    guide_world,
)

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from apps.accounts.models import User
    from apps.integrations.serpro import pgmei
else:
    pgmei = LazyModule("apps.integrations.serpro.pgmei")

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["guide_world"]


def obtain(
    world: GuideWorld, origin: str, actor: User | None = None
) -> pgmei.GuideOutcome:
    return pgmei.obtain_das_guide(
        actor or world.owner, world.tenant_id, world.cycle.pk, origin=origin
    )


def calls(world: GuideWorld) -> list[ProviderCall]:
    with tenant_context(world.tenant_id):
        return list(ProviderCall.objects.filter(operation="SP-DAS").order_by("pk"))


def test_owner_and_assigned_staff_obtain_but_operations_role_is_refused(
    guide_world: GuideWorld,
) -> None:
    # Given das.generate is FULL for staff and NONE for operations (matrix 108-112).
    with FakeSerpro((das_reply(),)) as peer:
        # When operations asks, then nothing is reserved and nothing is sent.
        with pytest.raises(PermissionDenied):
            obtain(guide_world, peer.origin, guide_world.ops)
        assert peer.requests == []
        assert calls(guide_world) == []
        # And an assigned staff accountant obtains the guide with one send.
        outcome = obtain(guide_world, peer.origin, guide_world.staff)
        assert outcome.code == "guia_obtida"
        assert len(peer.requests) == 1


def test_unassigned_staff_gets_404_with_no_row_written(
    guide_world: GuideWorld,
) -> None:
    with FakeSerpro((das_reply(),)) as peer:
        with pytest.raises(Http404):
            obtain(guide_world, peer.origin, guide_world.outsider)
        assert peer.requests == []
    assert calls(guide_world) == []
    with tenant_context(guide_world.tenant_id):
        assert not EvidenceRequest.objects.filter(title=pgmei.GUIDE_TITLE).exists()
        assert not Event.objects.filter(action=pgmei.REQUESTED_EVENT).exists()


def test_non_mei_profile_at_the_competence_is_refused_before_io(
    guide_world: GuideWorld,
) -> None:
    # Given the client's regime at the competence is no longer MEI.
    with tenant_context(guide_world.tenant_id):
        TaxProfile.objects.filter(client=guide_world.termo.client).update(
            regime="simples"
        )
        assert pgmei.guide_eligibility(guide_world.owner, guide_world.cycle) == (
            "nao_mei"
        )
    with FakeSerpro((das_reply(),)) as peer:
        with pytest.raises(pgmei.GuideRefused) as refused:
            obtain(guide_world, peer.origin)
        assert refused.value.code == "nao_mei"
        assert peer.requests == []
    assert calls(guide_world) == []


def test_flag_off_refuses_and_hides_the_action(guide_world: GuideWorld) -> None:
    tenant = Tenant.objects.get(pk=guide_world.tenant_id)
    tenant.features = [f for f in tenant.features if f != "federal_checks"]
    tenant.save(update_fields=["features"])
    with tenant_context(guide_world.tenant_id):
        assert pgmei.guide_eligibility(guide_world.owner, guide_world.cycle) == (
            "sinalizador_desligado"
        )
    with FakeSerpro((das_reply(),)) as peer:
        with pytest.raises(pgmei.GuideRefused):
            obtain(guide_world, peer.origin)
        assert peer.requests == []


def test_guide_is_private_quarantined_and_linked_on_a_firm_only_request(
    guide_world: GuideWorld,
) -> None:
    today = timezone.localdate()
    with FakeSerpro((das_reply(),)) as peer:
        outcome = obtain(guide_world, peer.origin)
        # Then exactly one documented /Emitir request names this period and today.
        assert [(r.method, r.path) for r in peer.requests] == [("POST", "/Emitir")]
        envelope = json.loads(peer.requests[0].body)
        assert envelope["pedidoDados"]["idSistema"] == "PGMEI"
        assert envelope["pedidoDados"]["idServico"] == "GERARDASPDF21"
        assert json.loads(envelope["pedidoDados"]["dados"]) == {
            "periodoApuracao": COMPETENCE,
            "dataConsolidacao": today.strftime("%Y%m%d"),
        }
        assert envelope["contribuinte"] == {"numero": "12ABC34501DE35", "tipo": 2}
    assert outcome.code == "guia_obtida"
    with tenant_context(guide_world.tenant_id):
        call = ProviderCall.objects.get(pk=outcome.call_id)
        assert (call.state, call.billed, call.http_status) == (
            CallState.SUCCESS,
            "sim",
            200,
        )
        document = Document.objects.get(pk=outcome.document_id)
        assert document.acquisition_origin == AcquisitionOrigin.FEDERAL_PROVIDER
        assert document.acquisition_receipt_ref == str(call.pk)
        assert (document.portal_visible, document.uploaded_by_id) == (False, None)
        assert document.content_type == "application/pdf"
        intake = DocumentIntake.objects.get(document=document)
        assert intake.state == IntakeState.QUARANTINED
        request = EvidenceRequest.objects.get(cycle=guide_world.cycle)
        assert request.title == pgmei.GUIDE_TITLE
        assert request.required is False
        assert request.state == pgmei.FIRM_ONLY_STATE
        assert request.state not in CLIENT_VISIBLE_STATES
        link = EvidenceLink.objects.get(pk=outcome.link_id)
        assert (link.request_id, link.document_id) == (request.pk, document.pk)
        assert link.origin == LinkOrigin.STAFF
        assert link.submitted_by_id == guide_world.owner.pk
        # Fake data never becomes an official observation (INV-R4).
        assert not SourceObservation.objects.filter(kind="guide_issued").exists()
        reservation = ProviderReservation.objects.get(call=call)
        assert (reservation.microbrl, reservation.state) == (PRICE_MICROBRL, "known")


def test_second_click_same_day_returns_the_same_guide_without_a_second_call(
    guide_world: GuideWorld,
) -> None:
    with FakeSerpro((das_reply(), das_reply())) as peer:
        first = obtain(guide_world, peer.origin)
        again = obtain(guide_world, peer.origin)
        assert len(peer.requests) == 1
    assert again.code == "guia_existente"
    assert (again.call_id, again.document_id, again.link_id) == (
        first.call_id,
        first.document_id,
        first.link_id,
    )
    assert len(calls(guide_world)) == 1


def test_a_later_consolidation_day_obtains_a_new_guide(
    guide_world: GuideWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    with FakeSerpro((das_reply(), das_reply(pdf=GUIDE_PDF + b"% next day\n"))) as peer:
        first = obtain(guide_world, peer.origin)
        tomorrow = timezone.localdate() + timedelta(days=1)
        monkeypatch.setattr(pgmei, "consolidation_day", lambda: tomorrow)
        second = obtain(guide_world, peer.origin)
        assert len(peer.requests) == 2
        assert json.loads(json.loads(peer.requests[1].body)["pedidoDados"]["dados"])[
            "dataConsolidacao"
        ] == tomorrow.strftime("%Y%m%d")
    assert second.call_id != first.call_id
    assert second.document_id != first.document_id
    with tenant_context(guide_world.tenant_id):
        # One firm-only request carries both guides.
        assert EvidenceRequest.objects.filter(cycle=guide_world.cycle).count() == 1
        assert (
            EvidenceLink.objects.filter(request__cycle=guide_world.cycle).count() == 2
        )


def test_firm_only_request_is_never_sent_to_the_client(
    guide_world: GuideWorld,
) -> None:
    with FakeSerpro((das_reply(),)) as peer:
        obtain(guide_world, peer.origin)
    with tenant_context(guide_world.tenant_id):
        cycle = guide_world.cycle
        cycle.refresh_from_db()
        # "Enviar solicitações" sees no draft: the guide request is not one.
        with pytest.raises(ValidationError):
            send_requests(guide_world.owner, cycle.pk, expected_version=cycle.version)
        assert not EvidenceRequest.objects.filter(
            cycle=cycle, state__in=CLIENT_VISIBLE_STATES
        ).exists()


def test_publication_form_lists_the_guide_link(guide_world: GuideWorld) -> None:
    with FakeSerpro((das_reply(),)) as peer:
        outcome = obtain(guide_world, peer.origin)
    with tenant_context(guide_world.tenant_id):
        form = ProposalForm()
        form.set_choices(guide_world.cycle)
        choices = dict(form.fields["document_links"].choices)  # type: ignore[attr-defined]
        assert str(outcome.link_id) in choices
        assert choices[str(outcome.link_id)].startswith(pgmei.GUIDE_FILENAME_PREFIX)


def test_budget_exhausted_is_refused_before_any_io(guide_world: GuideWorld) -> None:
    with tenant_context(guide_world.tenant_id):
        ProviderBudget.objects.update(cap_microbrl=0)
    with FakeSerpro((das_reply(),)) as peer:
        with pytest.raises(pgmei.GuideRefused) as refused:
            obtain(guide_world, peer.origin)
        assert refused.value.code == "cota_esgotada"
        assert peer.requests == []
    [call] = calls(guide_world)
    assert call.state == CallState.QUOTA
    assert call.attempts == 0
    with tenant_context(guide_world.tenant_id):
        assert ProviderBudget.objects.get().reserved == 0
        assert not Document.objects.filter(
            acquisition_origin=AcquisitionOrigin.FEDERAL_PROVIDER
        ).exists()


def test_only_the_guide_operation_of_pgmei_is_implementable() -> None:
    pgmei_services = {
        service for system, service in SERPRO_SERVICES.values() if system == "PGMEI"
    }
    assert pgmei_services == {"GERARDASPDF21"}
    # Never scheduled: no beat entry reaches the guide task.
    schedule = getattr(django_settings, "CELERY_BEAT_SCHEDULE", {}) or {}
    assert not any("das_guide" in str(entry) for entry in schedule.values())


def test_paused_provider_refuses_before_any_io(
    guide_world: GuideWorld, settings: SettingsWrapper
) -> None:
    settings.SERPRO_PAUSED = True
    with FakeSerpro((das_reply(),)) as peer:
        with pytest.raises(pgmei.GuideRefused) as refused:
            obtain(guide_world, peer.origin)
        assert refused.value.code == "provedor_pausado"
        assert peer.requests == []


def test_no_taxpayer_identifier_or_guide_bytes_reach_the_logs(
    guide_world: GuideWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    with FakeSerpro((das_reply(), Reply(status=400))) as peer:
        obtain(guide_world, peer.origin)
    text = caplog.text
    for canary in ("12ABC34501DE35", "07202630412345678", "JVBERi0x"):
        assert canary not in text


def test_competence_in_a_closed_month_still_uses_the_cycle_period(
    guide_world: GuideWorld,
) -> None:
    with tenant_context(guide_world.tenant_id):
        assert pgmei.competence(guide_world.cycle) == COMPETENCE
    assert pgmei.consolidation_day() == timezone.localdate()
    assert isinstance(pgmei.consolidation_day(), date)
