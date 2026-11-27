"""Reserve existing unsent work through the single provider protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from apps.core.tenancy import tenant_context
from apps.integrations.budget_models import ProviderReservation
from apps.integrations.call_contracts import CallRequest, IntentConflictError
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import reserve
from apps.integrations.focus.adapter import request_digest
from apps.integrations.focus.capture_wire import digest
from apps.integrations.transport_types import ProviderRequest
from tests.integrations.capture_fixtures import capture_world

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper


@pytest.mark.django_db(transaction=True)
def test_reserve_admits_a_previously_queued_call_once(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = capture_world(settings, tmp_path, monkeypatch)
    request = ProviderRequest()
    intent = CallRequest(
        "FC-NFE",
        digest("axrm022-queued-request"),
        request_digest(request),
        digest("axrm022-queued-effect"),
    )
    with tenant_context(world.tenant_id):
        queued = ProviderCall.objects.create(
            tenant_id=world.tenant_id,
            client_id=world.link.client_id,
            link=world.link,
            operation=intent.operation,
            operation_revision="v1",
            operation_class="read",
            provider="focus",
            environment="fake",
            request_key=intent.request_key,
            business_effect_key=intent.business_effect_key,
            request_digest=intent.request_digest,
            state=CallState.QUEUED,
            connection_version=world.link.connection.version,
            link_version=world.link.version,
            credential_generation=world.link.credential_generation,
            billing_class="incluido_no_plano",
        )
    admitted = reserve(world.tenant_id, world.link.pk, intent)
    assert admitted.pk == queued.pk
    assert admitted.state == CallState.RESERVED
    assert admitted.attempts == 0
    replay = reserve(world.tenant_id, world.link.pk, intent)
    assert replay.pk == admitted.pk
    with tenant_context(world.tenant_id):
        assert ProviderCall.objects.filter(request_key=intent.request_key).count() == 1
        assert ProviderReservation.objects.filter(call=admitted).count() == 1


@pytest.mark.django_db(transaction=True)
def test_reserve_refuses_a_queued_call_stored_under_another_tariff(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Added over 8f63bbf: a queued row is promoted only under its registered terms.
    world = capture_world(settings, tmp_path, monkeypatch)
    request = ProviderRequest()
    intent = CallRequest(
        "FC-NFE",
        digest("axrm022-tariff-request"),
        request_digest(request),
        digest("axrm022-tariff-effect"),
    )
    with tenant_context(world.tenant_id):
        queued = ProviderCall.objects.create(
            tenant_id=world.tenant_id,
            client_id=world.link.client_id,
            link=world.link,
            operation=intent.operation,
            operation_revision="v1",
            operation_class="read",
            provider="focus",
            environment="fake",
            request_key=intent.request_key,
            business_effect_key=intent.business_effect_key,
            request_digest=intent.request_digest,
            state=CallState.QUEUED,
            connection_version=world.link.connection.version,
            link_version=world.link.version,
            credential_generation=world.link.credential_generation,
            billing_class="nao_faturada",
        )
    outcome: object
    try:
        outcome = reserve(world.tenant_id, world.link.pk, intent)
    except Exception as error:  # noqa: BLE001 - the outcome is the assertion
        outcome = error
    assert isinstance(outcome, IntentConflictError), outcome
    with tenant_context(world.tenant_id):
        kept = ProviderCall.objects.get(pk=queued.pk)
        assert (kept.state, kept.billing_class, kept.attempts) == (
            CallState.QUEUED,
            "nao_faturada",
            0,
        )
        assert not ProviderReservation.objects.filter(call=kept).exists()
