"""SP-TERMO: a client-less, fenced and replayable firm submission (todo 9, A7.4)."""

import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from apps.accounts.models import User
from apps.clients.models import ClientCompany
from apps.core.tenancy import tenant_context
from apps.integrations import firm_calls, provider_journal
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallRequest,
    IntentConflictError,
    StaleClaimError,
)
from apps.integrations.call_models import (
    CallState,
    FirmProviderCall,
    FirmProviderCallAttempt,
)
from apps.integrations.firm_calls import (
    FirmOperation,
    firm_begin_io,
    firm_claim,
    firm_perform,
    firm_reserve,
    register_firm_operation,
)
from apps.integrations.models import FirmAuthorization, ProviderConnection
from apps.integrations.provider_journal import JournalError
from apps.obligations.models import AcquisitionOrigin, Document
from apps.tenants.models import Membership, Tenant, TenantRole
from tests.integrations.provider_peer import ProviderPeer

pytestmark = pytest.mark.django_db(transaction=True)


class TermoPolicy:
    """Synthetic firm-level authority (todo 13 supplies the real capability check)."""

    allowed = True

    def authorize(self, call: FirmProviderCall) -> bool:
        return self.allowed


@pytest.fixture
def termo(
    settings: SettingsWrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FirmAuthorization:
    settings.PROVIDERS_PAUSED = False
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setattr(firm_calls, "_FIRM_OPERATIONS", {})
    tenant = Tenant.objects.create(
        name="Termo synthetic",
        slug="termo-synthetic",
        features=["cycles_r1", "scan_required", "federal_checks"],
    )
    owner = User.objects.create_user(email="termo-owner@example.test")
    Membership.objects.create(tenant=tenant, user=owner, role=TenantRole.OWNER)
    with tenant_context(tenant.pk):
        client = ClientCompany.objects.create(
            tenant=tenant, legal_name="Synthetic", cnpj="11222333000181", is_mei=True
        )
        ProviderConnection.objects.create(
            tenant=tenant, provider="serpro", environment="fake", status="ativa"
        )
        document = Document.objects.create(
            tenant=tenant,
            client=client,
            original_filename="termo.xml",
            content_type="application/xml",
            byte_size=10,
            sha256="c" * 64,
            uploaded_by=owner,
            acquisition_origin=AcquisitionOrigin.UPLOAD_STAFF,
            portal_visible=False,
        )
        authorization = FirmAuthorization.objects.create(
            tenant=tenant,
            kind="termo_autorizacao_serpro",
            signed_document=document,
            submitted_at=timezone.now(),
            status="enviado",
        )
    register_firm_operation(
        FirmOperation(
            code="SP-TERMO", provider="serpro", revision="v1", policy=TermoPolicy()
        )
    )
    return authorization


def connection_id(termo: FirmAuthorization) -> UUID:
    with tenant_context(termo.tenant_id):
        return ProviderConnection.objects.get(provider="serpro").pk


def termo_request(key: str = "termo", *, body: str = "signed-termo") -> CallRequest:
    return CallRequest(
        operation="SP-TERMO",
        request_key=sha256(key.encode()).hexdigest(),
        request_digest=sha256(body.encode()).hexdigest(),
        business_effect_key=sha256(key.encode()).hexdigest(),
    )


def reserve(
    termo: FirmAuthorization, request: CallRequest | None = None
) -> FirmProviderCall:
    call = firm_reserve(
        termo.tenant_id, termo.pk, connection_id(termo), request or termo_request()
    )
    assert isinstance(call, FirmProviderCall)
    return call


def reload(termo: FirmAuthorization, call_id: UUID) -> FirmProviderCall:
    with tenant_context(termo.tenant_id):
        return FirmProviderCall.objects.get(pk=call_id)


def observations(termo: FirmAuthorization, call_id: UUID) -> list[tuple[str, str]]:
    with tenant_context(termo.tenant_id):
        return list(
            FirmProviderCallAttempt.objects.filter(call_id=call_id)
            .order_by("observation")
            .values_list("observation", "outcome")
        )


def restore_before_submission() -> None:
    """Drop the firm submission rows, keeping the independent journal intact."""
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        try:
            cursor.execute(
                "TRUNCATE integrations_firmprovidercallattempt, "
                "integrations_firmprovidercall"
            )
        finally:
            cursor.execute("SET ROLE app_runtime")


def reconcile() -> None:
    with override_settings(PROVIDERS_PAUSED=True):
        call_command("reconcile_provider_operations", restore_ref=str(uuid4()))


def test_termo_submission_has_no_client_column() -> None:
    # Given the firm submission model, then it cannot hold a client (A7.1 exemption).
    names = {field.name for field in FirmProviderCall._meta.get_fields()}
    assert "client" not in names
    assert {"authorization", "connection", "fence", "execution_epoch"} <= names


def test_duplicate_termo_request_returns_the_existing_submission(
    termo: FirmAuthorization,
) -> None:
    # Given a reserved termo submission.
    first = reserve(termo)
    # When the same deterministic request is delivered again.
    repeated = reserve(termo)
    # Then no second submission exists.
    assert isinstance(first, FirmProviderCall)
    assert repeated.pk == first.pk
    assert reload(termo, first.pk).state == CallState.RESERVED


def test_one_signed_termo_cannot_start_two_submissions(
    termo: FirmAuthorization,
) -> None:
    # Given one submission for the signed termo.
    reserve(termo)
    # When the same key carries other bytes, or another key reuses the termo.
    with pytest.raises(IntentConflictError):
        reserve(termo, termo_request(body="other-bytes"))
    with pytest.raises(IntentConflictError):
        reserve(termo, termo_request("another-key"))
    # Then the single submission stands.
    with tenant_context(termo.tenant_id):
        assert FirmProviderCall.objects.count() == 1


def test_termo_journal_records_no_client_link_or_call_ids(
    termo: FirmAuthorization,
) -> None:
    # Given a claimed termo submission and a synthetic SERPRO peer.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        # When the single transmission succeeds.
        reply = firm_perform(ticket, peer)
    # Then the call, its append-only result and both firm receipts are recorded.
    assert reply.state == CallState.SUCCESS
    assert peer.requests == [ticket.owner.hex]
    assert reload(termo, row.pk).state == CallState.SUCCESS
    assert observations(termo, row.pk) == [("result", CallState.SUCCESS)]
    records = provider_journal.read_firm(termo.tenant_id)
    assert sorted(r.result is not None for r in records) == [False, True]
    for record in records:
        assert record.path.startswith(f"provider-journal-firm/{termo.tenant_id}/")
        with default_storage.open(record.path, "rb") as stream:
            keys = set(json.loads(stream.read()))
        assert not keys & {"client_id", "link_id", "call_id"}
        assert {"authorization_id", "submission_id", "fence"} <= keys
    assert provider_journal.read(termo.tenant_id) == []


def test_stale_fence_or_expired_lease_never_transmits(
    termo: FirmAuthorization,
) -> None:
    # Given a claimed submission and an older fence for it.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    with pytest.raises(StaleClaimError):
        firm_begin_io(replace(ticket, fence=ticket.fence - 1))
    # When the worker's lease expires before dispatch.
    with tenant_context(termo.tenant_id):
        FirmProviderCall.objects.filter(pk=row.pk).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
    # Then it is uncertain and neither another claim nor the old ticket can start it.
    assert firm_claim(termo.tenant_id, row.pk) is None
    with pytest.raises(StaleClaimError):
        firm_begin_io(ticket)
    assert reload(termo, row.pk).state == CallState.UNCERTAIN


def test_pause_after_claim_refuses_termo_dispatch(
    termo: FirmAuthorization, settings: SettingsWrapper
) -> None:
    # Given a committed claim followed by the global pause.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    settings.PROVIDERS_PAUSED = True
    # When dispatch starts, then the live control refuses it before any receipt.
    with pytest.raises(AdmissionDeniedError):
        firm_begin_io(ticket)
    assert reload(termo, row.pk).state == CallState.PAUSED
    assert provider_journal.read_firm(termo.tenant_id) == []


def test_reset_after_termo_request_is_uncertain_and_never_retried(
    termo: FirmAuthorization,
) -> None:
    # Given a peer that receives the termo but resets before replying.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer(reset=True) as peer:
        reply = firm_perform(ticket, peer)
        # Then the registration is uncertain and no second transmission is admitted.
        assert reply.state == CallState.UNCERTAIN
        assert firm_claim(termo.tenant_id, row.pk) is None
        assert peer.requests == [ticket.owner.hex]
    assert reload(termo, row.pk).state == CallState.UNCERTAIN


def test_restore_rebuilds_the_termo_submission_without_another_request(
    termo: FirmAuthorization,
) -> None:
    # Given an accepted termo newer than the restored database horizon.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    with ProviderPeer() as peer:
        firm_perform(ticket, peer)
        restore_before_submission()
        # When the independent firm journal is replayed, twice.
        reconcile()
        reconcile()
        # Then the exact submission and its result are back and cannot run again.
        restored = reload(termo, row.pk)
        assert restored.state == CallState.SUCCESS
        assert restored.attempts == 1
        assert observations(termo, row.pk) == [("result", CallState.SUCCESS)]
        assert reserve(termo).pk == row.pk
        assert firm_claim(termo.tenant_id, row.pk) is None
        assert peer.requests == [ticket.owner.hex]


def test_start_only_termo_receipt_is_uncertain_after_restore(
    termo: FirmAuthorization,
) -> None:
    # Given an independent start receipt and a database restored before it.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    firm_begin_io(ticket)
    restore_before_submission()
    # When the journal is replayed.
    reconcile()
    # Then the registration is uncertain, never reclaimable.
    assert reload(termo, row.pk).state == CallState.UNCERTAIN
    assert observations(termo, row.pk) == [("uncertain", CallState.UNCERTAIN)]
    assert firm_claim(termo.tenant_id, row.pk) is None


def test_termo_claim_from_an_older_epoch_is_refused_at_dispatch(
    termo: FirmAuthorization,
) -> None:
    # Given a claim stamped before a restore advanced the execution epoch.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    reconcile()
    # When the stale worker reaches dispatch, then nothing is started or journaled.
    with pytest.raises(StaleClaimError):
        firm_begin_io(ticket)
    assert provider_journal.read_firm(termo.tenant_id) == []


def test_firm_receipt_carrying_a_client_id_is_refused(
    termo: FirmAuthorization,
) -> None:
    # Given a firm receipt object that smuggles a client identity.
    row = reserve(termo)
    ticket = firm_claim(termo.tenant_id, row.pk)
    assert ticket is not None
    firm_begin_io(ticket)
    (record,) = provider_journal.read_firm(termo.tenant_id)
    with default_storage.open(record.path, "rb") as stream:
        data = json.loads(stream.read())
    data["client_id"] = str(uuid4())
    default_storage.save(
        f"provider-journal-firm/{termo.tenant_id}/2026/10/{uuid4()}-{uuid4()}-start.json",
        ContentFile(json.dumps(data).encode()),
    )
    # When the journal is read, then the whole inventory fails closed.
    with pytest.raises(JournalError):
        provider_journal.read_firm(termo.tenant_id)
