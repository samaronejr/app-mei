"""Journaled company-token capture reads with protected, replayable responses."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import F
from django.utils import timezone

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import CallRequest
from apps.integrations.call_models import CallState, ProviderCall
from apps.integrations.call_protocol import claim, perform, reserve
from apps.integrations.focus.adapter import (
    FocusAdapter,
    request_digest,
    secret_variable,
)
from apps.integrations.focus.capture_lock import SharedTokenRateLimiter, TokenGuard
from apps.integrations.focus.company import _environment, _transport

if TYPE_CHECKING:
    from apps.integrations.call_contracts import CallResult
    from apps.integrations.call_dispatch import Dispatch
    from apps.integrations.models import ClientProviderLink
    from apps.integrations.sync_models import SyncRun
    from apps.integrations.transport import ProviderTransport
    from apps.integrations.transport_types import ProviderRequest


class CaptureReadError(Exception):
    """A minimized provider/protocol code; never carry response bytes or secrets."""

    def __init__(self, code: str) -> None:
        """Keep the bounded machine code for stream health."""
        self.code = code
        super().__init__(code)


class CaptureReadLimitedError(CaptureReadError):
    """Preparation can resume next hour without advancing this page."""


def digest(*parts: str) -> str:
    """Canonical identities for stream occurrences and read intents."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def company_secret_ref(link: ClientProviderLink) -> str:
    """Resolve the existing deployment-secret path for this company/environment."""
    return (
        f"{link.connection.secret_ref}/company/"
        f"{link.provider_company_ref}/{link.connection.environment}"
    )


def company_token(link: ClientProviderLink) -> str | None:
    """Read company credentials only; never fall back to the reseller secret."""
    if link.connection.environment == "fake":
        return f"synthetic-focus-company:{link.provider_company_ref}"
    return os.environ.get(secret_variable(company_secret_ref(link))) or None


def response_key(call: ProviderCall, *, page: bool) -> str:
    """Name a private object pinned to a journaled call, never a URL."""
    prefix, suffix = ("capture-pages", "json") if page else ("capture-xml", "xml")
    return f"{prefix}/{call.tenant_id.hex}/{call.pk.hex}.{suffix}"


class CaptureAdapter(FocusAdapter):
    """The Focus adapter with a shared send window and protected response storage."""

    def __init__(
        self,
        transport: ProviderTransport,
        request: ProviderRequest,
        *,
        token: str,
        guard: TokenGuard,
    ) -> None:
        """Bind the same in-memory company credential to ownership and admission."""
        super().__init__(transport, request, token=token, limiter_key=guard.key)
        self.guard = guard
        self.shared_limiter = SharedTokenRateLimiter()

    def _unsent(self, dispatch: Dispatch) -> CallResult | None:
        from apps.integrations.call_contracts import CallResult  # noqa: PLC0415

        self.guard.require_owned()
        if not self.matches(dispatch.receipt.request_digest):
            return CallResult(CallState.FAILED, None, "pedido_divergente", "nao")
        if self._token is None:
            return CallResult(CallState.FAILED, None, "credencial_ausente", "nao")
        if not self.shared_limiter.acquire(self._token):
            return CallResult(CallState.RETRY, None, "limite_compartilhado", "nao")
        return None

    def send(self, dispatch: Dispatch) -> CallResult:
        """Store the raw response before the successful call can finalize."""
        result = super().send(dispatch)
        if result.state == CallState.SUCCESS and self.response is not None:
            page = self.request.variant == "default"
            prefix, suffix = (
                ("capture-pages", "json") if page else ("capture-xml", "xml")
            )
            key = (
                f"{prefix}/{dispatch.receipt.tenant_id.hex}/"
                f"{dispatch.receipt.call_id.hex}.{suffix}"
            )
            if not default_storage.exists(key):
                stored = default_storage.save(key, ContentFile(self.response.body))
                if stored != key:
                    code = "objeto_divergente"
                    raise CaptureReadError(code)
        return result


def read_response(
    run: SyncRun,
    link: ClientProviderLink,
    request: ProviderRequest,
    *,
    identity: str,
    guard: TokenGuard,
) -> tuple[bytes, ProviderCall, str]:
    """Reuse XML only within one binding; page reads belong to one run/cursor."""
    assert_no_transaction()
    guard.require_owned()
    identity = digest(
        identity,
        json.dumps(run.binding_snapshot, sort_keys=True, separators=(",", ":")),
    )
    operation = "FC-NFE" if run.stream.collection == "nfe_recebidas" else "FC-NFS"
    is_page = request.variant == "default"
    with tenant_context(run.tenant_id):
        cached = (
            ProviderCall.objects.filter(
                link_id=link.pk,
                operation=operation,
                business_effect_key=identity,
                state=CallState.SUCCESS,
            )
            .order_by("created_at", "pk")
            .first()
        )
    if cached is None:
        if run.requests_made >= run.request_limit:
            code = "limite_de_requisicoes"
            raise CaptureReadLimitedError(code)
        call_request = CallRequest(
            operation,
            digest(identity, run.occurrence_key),
            request_digest(request),
            identity,
        )
        call = reserve(run.tenant_id, link.pk, call_request)
        ticket = claim(run.tenant_id, call.pk)
        if ticket is None:
            code = "chamada_em_andamento"
            raise CaptureReadError(code)
        with tenant_context(run.tenant_id):
            current = (
                type(run)
                .objects.filter(
                    pk=run.pk,
                    fence=run.fence,
                    lease_owner=run.lease_owner,
                    state="executando",
                    lease_until__gt=timezone.now(),
                )
                .update(
                    requests_made=F("requests_made") + 1,
                    lease_until=timezone.now() + timedelta(minutes=15),
                )
            )
        if not current:
            code = "execucao_substituida"
            raise CaptureReadError(code)
        run.requests_made += 1
        adapter = CaptureAdapter(
            _transport(_environment(link)), request, token=guard.token, guard=guard
        )
        result = perform(ticket, adapter)
        guard.require_owned()
        if result.state != CallState.SUCCESS:
            if result.provider_code == "limite_compartilhado":
                raise CaptureReadLimitedError(result.provider_code)
            raise CaptureReadError(result.provider_code or "leitura_falhou")
        with tenant_context(run.tenant_id):
            cached = ProviderCall.objects.get(pk=call.pk)
    key = response_key(cached, page=is_page)
    with default_storage.open(key, "rb") as stored:
        payload = stored.read()
    return payload, cached, key
