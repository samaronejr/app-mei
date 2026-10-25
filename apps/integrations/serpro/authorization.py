"""Capability-checked signed-termo submission through the client-less firm journal."""

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import Event
from apps.audit.services import ObjectRef, record_event
from apps.authz.objects import load_visible_client
from apps.authz.services import can
from apps.core.flags import tenant_row_has
from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.integrations.call_contracts import (
    AdmissionDeniedError,
    CallRequest,
    CallResult,
)
from apps.integrations.call_models import CallState
from apps.integrations.firm_calls import firm_claim, firm_perform, firm_reserve
from apps.integrations.models import (
    FirmAuthorization,
    FirmAuthorizationKind,
    FirmAuthorizationStatus,
    ProviderConnection,
)
from apps.integrations.serpro.adapter import Parties, SerproAdapter, build_request
from apps.integrations.serpro.broker_handoff import store_procurador
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.registration import (
    MANAGE,
    SUBMITTED,
    scanned_private_document,
)
from apps.integrations.serpro.termo import (
    MAX_XML,
    TermoInvalidoError,
    read_signed_termo,
)
from apps.integrations.serpro.termo_response import parse_termo_response
from apps.integrations.serpro.work import PROC_MISSING, UNCERTAIN, own_firm_condition
from apps.integrations.services import STALE_MESSAGE, IntegrationConflictError
from apps.integrations.transport import ProviderTransport
from apps.integrations.transport_types import Environment, JSONValue, TransportFailure
from apps.obligations.models import Document
from apps.tenants.models import Tenant


@dataclass(frozen=True, slots=True)
class TermoSubmission:
    """One rendered firm action and its immutable signed document reference."""

    connection_id: UUID
    document_id: UUID
    expected_version: int
    parties: Parties
    fake_origin: str = ""
    # A broker-restart re-submission of the already accepted termo. It is not a
    # rendered operator action, so it must not invalidate in-flight client calls
    # by advancing the connection version.
    automatic: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedTermo:
    authorization: FirmAuthorization
    environment: Environment
    data: JSONValue = field(repr=False)
    request: CallRequest
    valid_until: date


def _locked_document(
    actor: User, submission: TermoSubmission
) -> tuple[ProviderConnection, Document]:
    connection = ProviderConnection.objects.select_for_update().get(
        pk=submission.connection_id, provider="serpro"
    )
    if connection.version != submission.expected_version:
        raise IntegrationConflictError(STALE_MESSAGE)
    if not can(actor, MANAGE):
        raise PermissionDenied
    document = Document.objects.get(pk=submission.document_id)
    load_visible_client(actor, document.client_id)
    if not scanned_private_document(document):
        raise TermoInvalidoError
    return connection, document


def _prepare(
    actor: User, tenant_id: UUID, submission: TermoSubmission
) -> _PreparedTermo:
    with tenant_context(tenant_id):
        # PLATFORM_QUERY_OK: lock the explicitly named tenant's feature entitlement.
        tenant = Tenant.objects.select_for_update(no_key=True).get(pk=tenant_id)
        _, document = _locked_document(actor, submission)
        if (
            settings.PROVIDERS_PAUSED
            or settings.SERPRO_PAUSED
            or not tenant.is_active
            or not tenant_row_has(tenant, "federal_checks")
        ):
            raise AdmissionDeniedError(CallState.PAUSED)
    with default_storage.open(document.storage_key, "rb") as source:
        raw = source.read(MAX_XML + 1)
    if hashlib.sha256(raw).hexdigest() != document.sha256:
        raise TermoInvalidoError
    signed = read_signed_termo(raw, submission.parties)
    if (
        signed.signed_on > timezone.localdate()
        or signed.valid_until < timezone.localdate()
    ):
        with tenant_context(tenant_id):
            own_firm_condition(
                ProviderConnection.objects.get(pk=submission.connection_id),
                PROC_MISSING,
                [str(document.pk)],
            )
        raise TermoInvalidoError
    with tenant_context(tenant_id):
        connection, current = _locked_document(actor, submission)
        if current.sha256 != document.sha256:
            raise TermoInvalidoError
        authorization = FirmAuthorization.objects.create(
            tenant_id=tenant_id,
            kind=FirmAuthorizationKind.TERMO_AUTORIZACAO_SERPRO,
            signed_document=current,
            submitted_at=timezone.now(),
            valid_until=signed.valid_until,
            status=FirmAuthorizationStatus.ENVIADO,
        )
        record_event(
            action=SUBMITTED,
            actor=actor,
            tenant_id=tenant_id,
            obj=ObjectRef("FirmAuthorization", str(authorization.pk)),
            metadata={
                "document": str(current.pk),
                "connection": str(connection.pk),
                "automatic": submission.automatic,
            },
        )
        if not submission.automatic:
            connection.version += 1
            connection.save()
        environment: Environment
        match connection.environment:
            case "fake":
                environment = "fake"
            case "trial":
                environment = "trial"
            case "producao":
                environment = "producao"
            case _:
                raise TermoInvalidoError
    data: JSONValue = {"xml": base64.b64encode(raw).decode()}
    wire = build_request("SP-TERMO", submission.parties, data)
    identity = hashlib.sha256(
        f"{authorization.pk}:{submission.connection_id}".encode()
    ).hexdigest()
    request = CallRequest(
        "SP-TERMO", identity, hashlib.sha256(wire.body).hexdigest(), identity
    )
    return _PreparedTermo(authorization, environment, data, request, signed.valid_until)


def submit_termo(
    actor: User, tenant_id: UUID, submission: TermoSubmission
) -> CallResult:
    """Prepare scanned evidence, send outside locks, then append the fenced result."""
    assert_no_transaction()
    prepared = _prepare(actor, tenant_id, submission)
    authorization = prepared.authorization
    document = authorization.signed_document
    row = firm_reserve(
        tenant_id, authorization.pk, submission.connection_id, prepared.request
    )
    ticket = firm_claim(tenant_id, row.pk)
    if ticket is None:
        return CallResult(CallState.UNCERTAIN, None, "claim_indisponivel", "nao")
    adapter = SerproAdapter(
        ProviderTransport("serpro", prepared.environment, submission.fake_origin),
        submission.parties,
        prepared.data,
    )
    result = firm_perform(ticket, adapter)
    if result.state != CallState.SUCCESS:
        if result.state in {CallState.DENIED, CallState.FAILED}:
            with tenant_context(tenant_id):
                FirmAuthorization.objects.create(
                    tenant_id=tenant_id,
                    kind=authorization.kind,
                    signed_document=document,
                    submitted_at=authorization.submitted_at,
                    valid_until=prepared.valid_until,
                    status=FirmAuthorizationStatus.RECUSADO,
                )
                # A second 401 is already owned as autenticacao_serpro_recusada.
                if result.http_status != HTTPStatus.UNAUTHORIZED:
                    own_firm_condition(
                        ProviderConnection.objects.get(pk=submission.connection_id),
                        PROC_MISSING,
                        [str(row.pk)],
                    )
        return result
    if adapter.response is None:
        raise TermoInvalidoError
    try:
        token = parse_termo_response(adapter.response)
    except TransportFailure:
        # The HTTP receipt is final, but domain acceptance is still unresolved.
        with tenant_context(tenant_id):
            own_firm_condition(
                ProviderConnection.objects.get(pk=submission.connection_id),
                UNCERTAIN,
                [str(row.pk)],
            )
        raise
    valid_until = min(
        prepared.valid_until, datetime.fromtimestamp(token.expires_at, tz=UTC).date()
    )
    with tenant_context(tenant_id):
        ProviderConnection.objects.select_for_update().get(pk=submission.connection_id)
        accepted = FirmAuthorization.objects.create(
            tenant_id=tenant_id,
            kind=authorization.kind,
            signed_document=document,
            submitted_at=authorization.submitted_at,
            valid_until=valid_until,
            status=FirmAuthorizationStatus.ACEITO,
        )
        record_event(
            action="integrations.firm_authorization_accepted",
            tenant_id=tenant_id,
            obj=ObjectRef("FirmAuthorization", str(accepted.pk)),
            metadata={"submission": str(row.pk)},
        )
        for issue in FirmIntegrationIssue.objects.select_for_update().filter(
            connection_id=submission.connection_id,
            reason=PROC_MISSING,
            state__in=ACTIVE_STATES,
        ):
            issue.state = "resolvida"
            issue.version += 1
            issue.updated_at = timezone.now()
            issue.save()
            record_event(
                action="integrations.firm_issue_resolved",
                tenant_id=tenant_id,
                obj=ObjectRef("FirmIntegrationIssue", str(issue.pk)),
                metadata={"authorization": str(accepted.pk)},
            )
    if prepared.environment == "producao":
        store_procurador(submission.parties.firm, token)
    return result


def recover_procurador(tenant_id: UUID, connection_id: UUID, parties: Parties) -> None:
    """Recover from a broker restart through a new free signed-termo call."""
    assert_no_transaction()
    with tenant_context(tenant_id):
        connection = ProviderConnection.objects.get(pk=connection_id, provider="serpro")
        source = (
            Event.objects.select_related("actor")
            .filter(
                action=SUBMITTED,
                metadata__connection=str(connection_id),
                actor__isnull=False,
                object_type="FirmAuthorization",
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if source is None or source.actor is None:
            code = PROC_MISSING
            raise TransportFailure(code)
        authorization = FirmAuthorization.objects.get(pk=source.object_id)
        submission = TermoSubmission(
            connection_id,
            authorization.signed_document_id,
            connection.version,
            parties,
            automatic=True,
        )
        actor = source.actor
    result = submit_termo(actor, tenant_id, submission)
    if result.state != CallState.SUCCESS:
        code = PROC_MISSING
        raise TransportFailure(code)
