"""Which firm termo governs a SERPRO connection, from settled outcomes only."""

from datetime import date
from uuid import UUID

from django.db.models import Exists, OuterRef

from apps.integrations.call_models import CallState, FirmProviderCall
from apps.integrations.models import FirmAuthorization, FirmAuthorizationStatus


def termo_head(connection_id: UUID) -> FirmAuthorization | None:
    """Return the latest settled outcome of a termo submitted through this connection.

    `FirmAuthorization` is append-only: a submission writes `enviado`, then its
    outcome. An in-flight or uncertain submission (still `enviado`) never changes
    which termo is in force, and another environment's connection never counts.
    """
    through_connection = FirmProviderCall.objects.filter(
        connection_id=connection_id,
        authorization__signed_document_id=OuterRef("signed_document_id"),
        authorization__submitted_at=OuterRef("submitted_at"),
    )
    return (
        FirmAuthorization.objects.filter(Exists(through_connection))
        .exclude(status=FirmAuthorizationStatus.ENVIADO)
        .order_by("-submitted_at", "-created_at", "-pk")
        .first()
    )


def termo_in_force(connection_id: UUID, today: date) -> bool:
    """Report whether an accepted, unexpired termo submitted here is in force."""
    head = termo_head(connection_id)
    return (
        head is not None
        and head.status == FirmAuthorizationStatus.ACEITO
        and head.valid_until is not None
        and head.valid_until >= today
        and FirmProviderCall.objects.filter(
            connection_id=connection_id,
            authorization__signed_document_id=head.signed_document_id,
            authorization__submitted_at=head.submitted_at,
            state=CallState.SUCCESS,
        ).exists()
    )
