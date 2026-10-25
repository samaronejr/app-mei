"""Closed SERPRO operation registration with live authority and budget policies."""

from typing import Final, Literal

from django.utils import timezone

from apps.audit.models import Event
from apps.authz.services import can
from apps.cycles.evidence import CLEAN_STATES, ScanVerdict
from apps.integrations.authority import authority_for
from apps.integrations.budget import BudgetPolicy
from apps.integrations.call_contracts import Operation, register_operation
from apps.integrations.call_models import FirmProviderCall, ProviderCall
from apps.integrations.firm_calls import FirmOperation, register_firm_operation
from apps.integrations.models import FirmAuthorizationStatus
from apps.integrations.serpro.firm_budget import FirmBudgetPolicy
from apps.integrations.serpro.models import ACTIVE_STATES, FirmIntegrationIssue
from apps.integrations.serpro.termo_state import termo_in_force
from apps.obligations.models import AcquisitionOrigin, Document

SUBMITTED: Final = "integrations.firm_authorization_submitted"
MANAGE: Final = "fiscal.authority.manage"
CATALOG: Final[
    tuple[
        tuple[
            str,
            Literal["issuance", "read"],
            Literal["nao_faturada", "emissao", "consulta"],
        ],
        ...,
    ]
] = (
    ("SP-SIT-P", "issuance", "nao_faturada"),
    ("SP-SIT-R", "issuance", "emissao"),
    ("SP-PAG", "read", "consulta"),
    ("SP-COMP", "issuance", "emissao"),
    ("SP-DAS", "issuance", "emissao"),
)


def scanned_private_document(document: Document) -> bool:
    """Require a clean staff-intake scan before reading a signature."""
    return (
        not document.portal_visible
        and document.acquisition_origin == AcquisitionOrigin.UPLOAD_STAFF
        and document.purged_at is None
        and document.intakes.filter(
            state__in=CLEAN_STATES,
            verdict=ScanVerdict.CLEAN,
            scanned_at__isnull=False,
        ).exists()
    )


def firm_authority(call: FirmProviderCall) -> bool:
    """Use the immutable submit event to recheck the actual initiator under locks."""
    authorization = call.authorization
    event = (
        Event.objects.select_related("actor")
        .filter(
            action=SUBMITTED,
            object_type="FirmAuthorization",
            object_id=str(authorization.pk),
            metadata__connection=str(call.connection_id),
        )
        .first()
    )
    return (
        event is not None
        and event.actor is not None
        and can(event.actor, MANAGE, tenant_id=call.tenant_id)
        and authorization.status == FirmAuthorizationStatus.ENVIADO
        and authorization.valid_until is not None
        and authorization.valid_until >= timezone.localdate()
        and scanned_private_document(authorization.signed_document)
    )


def client_authority(call: ProviderCall) -> bool:
    """Both the firm's accepted termo and the client's service authority must hold."""
    connection_id = call.link.connection_id
    if FirmIntegrationIssue.objects.filter(
        connection_id=connection_id,
        reason="autorizacao_procurador_ausente",
        state__in=ACTIVE_STATES,
    ).exists():
        return False
    return (
        termo_in_force(connection_id, timezone.localdate())
        and authority_for(call.client, call.operation, link=call.link).usable
    )


def register_serpro_operations() -> None:
    """Register no declarations and no permissive generic provider operation."""
    for code, kind, billing in CATALOG:
        register_operation(
            Operation(
                code,
                "serpro",
                "v1",
                kind,
                "federal_checks",
                billing,
                BudgetPolicy(client_authority),
            )
        )
    register_firm_operation(
        FirmOperation("SP-TERMO", "serpro", "v1", FirmBudgetPolicy(firm_authority))
    )
