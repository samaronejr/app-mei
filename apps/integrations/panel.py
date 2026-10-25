"""The read-only "Conexoes fiscais" panel of the firm settings page (plan A9).

Shown only while a regional flag is effectively on and only to holders of
`fiscal.connections.manage`. Its status column answers "can this connection make a
call right now": the platform pause wins over the row, and nothing renders as
`confirmed` -- a connection is configuration, not verified evidence (A7.7).
"""

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final
from uuid import UUID

from django.conf import settings

from apps.accounts.models import User
from apps.authz.services import can
from apps.core.flags import tenant_has
from apps.integrations.models import ConnectionStatus, Provider, ProviderConnection

REGIONAL_FLAGS: Final = ("federal_checks", "document_capture", "municipal_work")
MANAGE_CAPABILITY: Final = "fiscal.connections.manage"


class BadgeState(StrEnum):
    """The states partials/ui/status_badge.html draws that this panel uses."""

    PENDING = auto()
    BLOCKED = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class ConnectionRow:
    """One connection as the panel draws it."""

    provider_label: str
    environment: str
    environment_label: str
    state: str
    status_label: str


def _provider_paused(provider: str) -> bool:
    if settings.PROVIDERS_PAUSED:
        return True
    if provider == Provider.SERPRO:
        return bool(settings.SERPRO_PAUSED)
    return provider == Provider.FOCUS and bool(settings.FOCUS_PAUSED)


def _status(connection: ProviderConnection) -> tuple[str, str]:
    """Return the badge state and its pt-BR label; never `confirmed`."""
    if connection.status == ConnectionStatus.INATIVA:
        return BadgeState.UNKNOWN, str(ConnectionStatus.INATIVA.label)
    if _provider_paused(connection.provider):
        return BadgeState.BLOCKED, "Pausada pela plataforma"
    if connection.status == ConnectionStatus.ATIVA:
        return BadgeState.PENDING, str(ConnectionStatus.ATIVA.label)
    return BadgeState.BLOCKED, connection.get_status_display()


def fiscal_connections_panel(user: User, tenant_id: UUID) -> list[ConnectionRow] | None:
    """Return the panel's rows, or None when this user must not see the panel."""
    if not any(tenant_has(tenant_id, flag) for flag in REGIONAL_FLAGS):
        return None
    if not can(user, MANAGE_CAPABILITY, tenant_id=tenant_id):
        return None
    rows = []
    # The scoped manager and the tenant RLS policy confine this to the request's firm.
    for connection in ProviderConnection.objects.order_by("provider", "environment"):
        state, label = _status(connection)
        rows.append(
            ConnectionRow(
                provider_label=connection.get_provider_display(),
                environment=connection.environment,
                environment_label=connection.get_environment_display(),
                state=state,
                status_label=label,
            )
        )
    return rows
