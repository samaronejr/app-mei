"""The provider protocol's public DB/I/O boundaries (AXRM-009, A7.4)."""

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.db import connection
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations import call_contracts
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallRequest,
    CallResult,
    IntentConflictError,
    Operation,
    Reservation,
    StaleClaimError,
    register_operation,
)
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import begin_io, claim, finalize, reserve
from apps.integrations.call_state import current_epoch
from apps.integrations.models import ClientProviderLink, ProviderConnection
from apps.tenants.models import Membership, Tenant, TenantRole

pytestmark = pytest.mark.django_db(transaction=True)


class SyntheticPolicy:
    """An explicit synthetic authority/tariff, not a production admission default."""

    allowed = True

    def authorize(self, call: ProviderCall) -> bool:
        return self.allowed

    def reserve(self, call: ProviderCall) -> Reservation:
        return Reservation(320_000, "synthetic-v1")

    def settle(self, call: ProviderCall, result: CallResult) -> None:
        self.settled.append(result.billed)

    def __init__(self) -> None:
        self.settled: list[str] = []


@pytest.fixture
def provider_link(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ClientProviderLink:
    settings.PROVIDERS_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setattr(call_contracts, "_OPERATIONS", {})
    tenant = Tenant.objects.create(
        name="Protocol synthetic",
        slug="protocol-synthetic",
        features=["cycles_r1", "scan_required", "federal_checks"],
    )
    owner = User.objects.create_user(email="protocol-owner@example.test")
    Membership.objects.create(tenant=tenant, user=owner, role=TenantRole.OWNER)
    with tenant_context(tenant.pk):
        client = ClientCompany.objects.create(
            tenant=tenant, legal_name="Synthetic", cnpj="11222333000181", is_mei=True
        )
        connection_row = ProviderConnection.objects.create(
            tenant=tenant, provider="serpro", environment="fake", status="ativa"
        )
        link = ClientProviderLink.objects.create(
            tenant=tenant,
            client=client,
            connection=connection_row,
            provider_company_ref="synthetic-company",
            status="ativa",
        )
    register_operation(
        Operation(
            code="SP-DAS",
            provider="serpro",
            revision="v1",
            operation_class="issuance",
            feature="federal_checks",
            billing_class="emissao",
            policy=SyntheticPolicy(),
        )
    )
    assert current_epoch() == 1
    return link


def request(key: str = "one", *, body: str = "payload") -> CallRequest:
    return CallRequest(
        operation="SP-DAS",
        request_key=sha256(key.encode()).hexdigest(),
        request_digest=sha256(body.encode()).hexdigest(),
        business_effect_key=sha256(key.encode()).hexdigest(),
    )


def reload_call(link: ClientProviderLink, call_id: UUID) -> ProviderCall:
    with tenant_context(link.tenant_id):
        return ProviderCall.objects.get(pk=call_id)


def test_duplicate_request_returns_existing_call(
    provider_link: ClientProviderLink,
) -> None:
    # Given a reserved provider request.
    first = reserve(provider_link.tenant_id, provider_link.pk, request())
    # When its deterministic identity is delivered again.
    repeated = reserve(provider_link.tenant_id, provider_link.pk, request())
    # Then no second reservation or call row is created.
    assert repeated.pk == first.pk
    assert repeated.reserved_microbrl == 320_000
    assert reload_call(provider_link, first.pk).state == CallState.RESERVED


def test_same_key_with_different_digest_conflicts(
    provider_link: ClientProviderLink,
) -> None:
    # Given an existing request key and digest.
    reserve(provider_link.tenant_id, provider_link.pk, request())
    # When the caller attempts to reuse it for different bytes.
    with pytest.raises(IntentConflictError):
        reserve(provider_link.tenant_id, provider_link.pk, request(body="different"))


def test_expired_claim_becomes_uncertain_and_is_not_reclaimed(
    provider_link: ClientProviderLink,
) -> None:
    # Given a claimed call whose worker stopped.
    call = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, call.pk)
    assert ticket is not None
    with tenant_context(provider_link.tenant_id):
        ProviderCall.objects.filter(pk=call.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    # When another worker tries to claim or start it.
    assert claim(provider_link.tenant_id, call.pk) is None
    with pytest.raises(StaleClaimError):
        begin_io(ticket)
    # Then uncertainty, rather than another transmission, is recorded.
    assert reload_call(provider_link, call.pk).state == CallState.UNCERTAIN


def test_stale_fence_cannot_start_or_finalize(
    provider_link: ClientProviderLink,
) -> None:
    # Given a committed current claim and an older fence.
    call = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, call.pk)
    assert ticket is not None
    stale = replace(ticket, fence=ticket.fence - 1, owner=uuid4())
    # When the stale worker reaches either DB/I/O boundary.
    with pytest.raises(StaleClaimError):
        begin_io(stale)
    assert not finalize(stale, CallResult(CallState.SUCCESS, 200, "ok", "sim"))
    # Then the live claim is untouched.
    assert reload_call(provider_link, call.pk).state == CallState.CLAIMED
    assert not connection.in_atomic_block


def test_dispatch_rechecks_a_pause_after_claim(
    provider_link: ClientProviderLink, settings: SettingsWrapper
) -> None:
    # Given a committed claim followed by the owner's pause.
    row = reserve(provider_link.tenant_id, provider_link.pk, request())
    ticket = claim(provider_link.tenant_id, row.pk)
    assert ticket is not None
    settings.PROVIDERS_PAUSED = True
    # When the worker reaches dispatch, the earlier claim grants no stale permission.
    with pytest.raises(AdmissionDeniedError):
        begin_io(ticket)
    # Then resuming cannot revive the already-refused dispatch ticket.
    settings.PROVIDERS_PAUSED = False
    with pytest.raises(StaleClaimError):
        begin_io(ticket)
    assert reload_call(provider_link, row.pk).state == CallState.PAUSED


@pytest.mark.parametrize(
    "code",
    [
        "Authorization: Bearer synthetic-canary",
        "https://example.invalid/?token=synthetic-canary",
        "A" * 65,
        "ok\nsynthetic-canary",
    ],
)
def test_response_codes_cannot_carry_untrusted_text_or_secrets(code: str) -> None:
    # Given untrusted adapter text rather than a symbolic provider code.
    # When it crosses the protocol boundary.
    with pytest.raises(ValueError, match="Invalid minimized"):
        CallResult(CallState.SUCCESS, 200, code, "sim")
    # Then none of it can reach a DB attempt, journal object or success log.
