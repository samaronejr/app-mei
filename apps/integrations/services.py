"""Locked, version-checked state changes for connections and client provider links.

Every change follows the same order: open the firm's tenant context, lock the row
(`select_for_update`), compare the caller's rendered `version`, check the transition
against its A7.3 machine, then write the new state, bump `version` and record an audit
event -- all in one transaction. A stale version or a transition the machine does not
allow raises before anything is written.

These are the system and operator paths (later todos' commands and sweeps call them).
They take no actor and so check no capability: the firm-facing actions of todo 34 wrap
them with their own `can()` re-check inside the same lock.
"""

import datetime as dt
from collections.abc import Mapping
from enum import StrEnum
from typing import Final
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import ObjectRef, record_event
from apps.core.tenancy import tenant_context
from apps.integrations.models import (
    ClientProviderLink,
    ConnectionStatus,
    CredentialState,
    DisablementState,
    LinkStatus,
    ProviderConnection,
)

STALE_MESSAGE: Final = (
    "A conexão mudou depois que você abriu a página. Nada foi salvo. "
    "Recarregue antes de tentar novamente."
)


class IntegrationConflictError(Exception):
    """The caller's rendered version is stale; nothing was written."""


class InvalidTransitionError(Exception):
    """The requested state is unreachable from the current one; nothing written."""


class CaptureStartImmutableError(Exception):
    """The capture start month is already set and never changes (INV-R11)."""


class LinkMachine(StrEnum):
    """The three independent state fields of a client provider link (A7.3)."""

    STATUS = "status"
    CREDENTIAL = "credential_state"
    DISABLEMENT = "disablement_state"


LINK_TRANSITIONS: Final[Mapping[LinkMachine, Mapping[str, frozenset[str]]]] = {
    LinkMachine.STATUS: {
        LinkStatus.CONFIGURACAO_PENDENTE: frozenset(
            {LinkStatus.ATIVA, LinkStatus.REVOGADA}
        ),
        LinkStatus.ATIVA: frozenset({LinkStatus.REVOGADA}),
        LinkStatus.REVOGADA: frozenset(),
    },
    LinkMachine.CREDENTIAL: {
        CredentialState.AUSENTE: frozenset({CredentialState.CONFIRMACAO_PENDENTE}),
        CredentialState.CONFIRMACAO_PENDENTE: frozenset(
            {CredentialState.ATIVA, CredentialState.DIVERGENTE}
        ),
        CredentialState.ATIVA: frozenset(
            {
                CredentialState.VENCENDO,
                CredentialState.VENCIDA,
                CredentialState.ROTACAO_PENDENTE,
                CredentialState.DIVERGENTE,
            }
        ),
        CredentialState.VENCENDO: frozenset(
            {
                CredentialState.VENCIDA,
                CredentialState.ROTACAO_PENDENTE,
                CredentialState.DIVERGENTE,
            }
        ),
        CredentialState.VENCIDA: frozenset(
            {CredentialState.ROTACAO_PENDENTE, CredentialState.DIVERGENTE}
        ),
        CredentialState.ROTACAO_PENDENTE: frozenset(
            {CredentialState.ATIVA, CredentialState.DIVERGENTE}
        ),
        CredentialState.DIVERGENTE: frozenset({CredentialState.CONFIRMACAO_PENDENTE}),
    },
    LinkMachine.DISABLEMENT: {
        DisablementState.ATIVA: frozenset({DisablementState.PAUSA_SOLICITADA}),
        DisablementState.PAUSA_SOLICITADA: frozenset(
            {DisablementState.PAUSADA_LOCALMENTE}
        ),
        DisablementState.PAUSADA_LOCALMENTE: frozenset(
            {DisablementState.DESATIVACAO_NO_PROVEDOR_PENDENTE}
        ),
        DisablementState.DESATIVACAO_NO_PROVEDOR_PENDENTE: frozenset(
            {DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA}
        ),
        DisablementState.DESATIVADA_NO_PROVEDOR_CONFIRMADA: frozenset(),
    },
}


def _check_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise IntegrationConflictError(STALE_MESSAGE)


def _refuse(machine: str, current: str, to: str) -> InvalidTransitionError:
    return InvalidTransitionError(f"{machine}: {current!r} -> {to!r} is not allowed")


def transition_link(
    tenant_id: UUID,
    link_id: UUID,
    *,
    machine: LinkMachine,
    to: str,
    expected_version: int,
) -> ClientProviderLink:
    """Move one of a link's three state fields along its machine, or write nothing."""
    with tenant_context(tenant_id), transaction.atomic():
        link = ClientProviderLink.objects.select_for_update().get(pk=link_id)
        _check_version(link.version, expected_version)
        current = str(getattr(link, machine.value))
        if to not in LINK_TRANSITIONS[machine].get(current, frozenset()):
            raise _refuse(machine.value, current, to)
        setattr(link, machine.value, to)
        if (
            machine is LinkMachine.CREDENTIAL
            and current == CredentialState.ROTACAO_PENDENTE
            and to == CredentialState.ATIVA
        ):
            link.credential_generation += 1
        link.version += 1
        link.updated_at = timezone.now()
        link.save()
        record_event(
            action="integrations.link_state_changed",
            tenant_id=tenant_id,
            obj=ObjectRef("ClientProviderLink", str(link.pk)),
            metadata={
                "field": machine.value,
                "from": current,
                "to": to,
                "version": link.version,
                "credential_generation": link.credential_generation,
            },
        )
        return link


def set_capture_start_month(
    tenant_id: UUID,
    link_id: UUID,
    *,
    month: dt.date,
    expected_version: int,
) -> ClientProviderLink:
    """Record the approved capture start month once; it never changes afterwards."""
    if month.day != 1:
        message = "O início da captura é sempre o primeiro dia de um mês."
        raise ValidationError(message, code="capture_start_month")
    with tenant_context(tenant_id), transaction.atomic():
        link = ClientProviderLink.objects.select_for_update().get(pk=link_id)
        _check_version(link.version, expected_version)
        if link.capture_start_month is not None:
            message = "O mês de início da captura já foi definido e não muda."
            raise CaptureStartImmutableError(message)
        link.capture_start_month = month
        link.version += 1
        link.updated_at = timezone.now()
        link.save()
        record_event(
            action="integrations.capture_start_set",
            tenant_id=tenant_id,
            obj=ObjectRef("ClientProviderLink", str(link.pk)),
            metadata={"month": month.isoformat(), "version": link.version},
        )
        return link


def transition_connection_status(
    tenant_id: UUID,
    connection_id: UUID,
    *,
    to: str,
    expected_version: int,
) -> ProviderConnection:
    """Change a connection's status to a different known one, or write nothing."""
    with tenant_context(tenant_id), transaction.atomic():
        connection = ProviderConnection.objects.select_for_update().get(
            pk=connection_id
        )
        _check_version(connection.version, expected_version)
        current = connection.status
        if to not in ConnectionStatus.values or to == current:
            field = "status"
            raise _refuse(field, current, to)
        connection.status = to
        connection.version += 1
        connection.updated_at = timezone.now()
        connection.save()
        record_event(
            action="integrations.connection_status_changed",
            tenant_id=tenant_id,
            obj=ObjectRef("ProviderConnection", str(connection.pk)),
            metadata={"from": current, "to": to, "version": connection.version},
        )
        return connection
