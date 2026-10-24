"""Focus's included calls and monthly document estimates are different units."""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts, provider_journal
from apps.integrations.budget import BudgetPolicy
from apps.integrations.budget_models import ProviderBudget, ProviderReservation
from apps.integrations.budget_pricing import FOCUS_PLAN
from apps.integrations.call_contracts import AdmissionDeniedError, Operation
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.focus_usage import CapturedRevision, record_captured_revision
from apps.integrations.models import (
    ClientProviderLink,
    IntegrationIssue,
    ProviderConnection,
)
from apps.tenants.models import Tenant, UsageKind, UsageRecord
from tests.integrations.provider_peer import ProviderPeer
from tests.integrations.test_call_protocol import provider_link, request

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["provider_link"]


@pytest.fixture
def focus_link(
    provider_link: ClientProviderLink, settings: SettingsWrapper
) -> ClientProviderLink:
    settings.USAGE_UNIT_PRICES_BRL = {"focus_documentos": "1"}
    Tenant.objects.filter(pk=provider_link.tenant_id).update(
        features=["cycles_r1", "scan_required", "document_capture"]
    )
    with tenant_context(provider_link.tenant_id):
        ProviderConnection.objects.filter(pk=provider_link.connection_id).update(
            provider="focus"
        )
        ClientProviderLink.objects.filter(pk=provider_link.pk).update(
            credential_state="ativa"
        )
        ProviderBudget.objects.create(
            tenant_id=provider_link.tenant_id,
            provider="focus",
            window_start=timezone.now().date().replace(day=1),
            cap_microbrl=5_000_000,
            included_documents=1,
            tariff_revision=FOCUS_PLAN,
        )
    call_contracts.register_operation(
        Operation(
            "FC-NFE",
            "focus",
            "v1",
            "read",
            "document_capture",
            "incluido_no_plano",
            BudgetPolicy(lambda call: True),
        )
    )
    return provider_link


def test_zero_focus_tariff_requires_the_explicit_provisional_revision(
    focus_link: ClientProviderLink,
) -> None:
    # Given a Focus budget whose tariff revision is not a recorded zero entry.
    with tenant_context(focus_link.tenant_id):
        ProviderBudget.objects.update(tariff_revision="focus-plan-v2")
    # When an included-plan read asks for its reservation.
    with pytest.raises(AdmissionDeniedError, match="tarifa_ausente"):
        reserve(
            focus_link.tenant_id,
            focus_link.pk,
            replace(request("unknown-tariff"), operation="FC-NFE"),
        )
    # Then an unrecorded revision is an absent price, never an implicit zero.
    with tenant_context(focus_link.tenant_id):
        assert not ProviderReservation.objects.exists()
        assert ProviderCall.objects.get().state == CallState.QUOTA


def test_repeated_document_calls_and_revisions_do_not_double_count(
    focus_link: ClientProviderLink,
) -> None:
    # Given repeated reads of one captured document and two revisions this month.
    revision = CapturedRevision(
        focus_link.tenant_id, focus_link.client_id, uuid4(), uuid4(), timezone.now()
    )
    with ProviderPeer() as peer:
        for key in ("page", "xml", "retry"):
            call = reserve(
                focus_link.tenant_id,
                focus_link.pk,
                replace(request(key), operation="FC-NFE"),
            )
            assert call.reserved_microbrl == 0
            assert call.tariff_revision == FOCUS_PLAN
            ticket = claim(focus_link.tenant_id, call.pk)
            assert ticket is not None
            perform(ticket, peer)
    # When the register reports the revisions again.
    assert record_captured_revision(focus_link.pk, revision)
    assert not record_captured_revision(
        focus_link.pk, replace(revision, revision_id=uuid4())
    )
    # Then all calls remain journaled but only one monthly document unit exists.
    assert len(provider_journal.read(focus_link.tenant_id)) == 6
    with tenant_context(focus_link.tenant_id):
        row = UsageRecord.objects.get(metric="focus_documentos")
        assert (row.quantity, row.kind) == (1, UsageKind.ESTIMATED)
        assert ProviderBudget.objects.get().uncertain_exposure == 0


def test_focus_overage_keeps_actual_usage_and_opens_threshold_work(
    focus_link: ClientProviderLink,
) -> None:
    # Given one included document and a five-unit Axorim overage limit.
    # When seven documents are observed despite our local threshold.
    for _ in range(7):
        record_captured_revision(
            focus_link.pk,
            CapturedRevision(
                focus_link.tenant_id,
                focus_link.client_id,
                uuid4(),
                uuid4(),
                timezone.now(),
            ),
        )
    # Then both thresholds were observed and evidence beyond the cap is retained.
    with tenant_context(focus_link.tenant_id):
        assert UsageRecord.objects.filter(metric="focus_documentos").count() == 7
        assert ProviderBudget.objects.get().uncertain_exposure == 6_000_000
        assert set(IntegrationIssue.objects.values_list("reason", flat=True)) == {
            "cota_provedor_proxima",
            "cota_provedor_esgotada",
        }


def test_included_configuration_can_disable_capture_after_overage(
    focus_link: ClientProviderLink,
) -> None:
    # Given provider-side capture has exceeded the local warning threshold.
    with tenant_context(focus_link.tenant_id):
        ProviderBudget.objects.update(uncertain_exposure=6_000_000)
    call_contracts.register_operation(
        replace(
            call_contracts.operation_for("FC-NFE"),
            code="FC-EMP",
            operation_class="configuration",
        )
    )
    # When the operator requests the zero-cost configuration operation.
    call = reserve(
        focus_link.tenant_id,
        focus_link.pk,
        replace(request("disable"), operation="FC-EMP"),
    )
    ticket = claim(focus_link.tenant_id, call.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        perform(ticket, peer)
        assert len(peer.requests) == 1
    # Then exhausted funds do not prevent a journaled upstream disable action.
    assert len(provider_journal.read(focus_link.tenant_id)) == 2
    with tenant_context(focus_link.tenant_id):
        assert ProviderBudget.objects.get().uncertain_exposure == 6_000_000


def test_ciencia_is_an_estimated_event_not_a_document(
    focus_link: ClientProviderLink,
) -> None:
    # Given an exact-authorized synthetic Ciencia registration.
    call_contracts.register_operation(
        replace(
            call_contracts.operation_for("FC-NFE"),
            code="FC-CIE",
            operation_class="registration",
        )
    )
    # When one registration is transmitted and its result is finalized.
    call = reserve(
        focus_link.tenant_id,
        focus_link.pk,
        replace(request("ciencia"), operation="FC-CIE"),
    )
    ticket = claim(focus_link.tenant_id, call.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        perform(ticket, peer)
    # Then the provisional unknown event tariff remains estimated, not free evidence.
    with tenant_context(focus_link.tenant_id):
        row = UsageRecord.objects.get()
        assert (row.metric, row.kind, row.quantity) == ("focus_eventos", "estimated", 1)
        assert ProviderBudget.objects.get().known_liability == 0
