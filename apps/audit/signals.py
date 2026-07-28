"""Identity events, recorded where they actually happen.

Every receiver here writes a `PlatformEvent` rather than an `Event`. That is not a
stylistic choice: at each of these moments no tenant has been resolved, so an `Event`
INSERT would be rejected by its own `WITH CHECK` predicate and the security record
would be lost exactly when it matters.
"""

from typing import Any

from allauth.mfa.signals import (
    authenticator_added,
    authenticator_removed,
    authenticator_reset,
)
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver
from django.http import HttpRequest

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import Origin, record_platform_event
from apps.core.netaddr import client_ip


def _ip(request: HttpRequest | None) -> str | None:
    return client_ip(request) if request is not None else None


@receiver(user_logged_in)
def on_login(
    sender: object,  # noqa: ARG001
    request: HttpRequest | None = None,
    user: User | None = None,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Record a successful authentication."""
    record_platform_event(
        action=AuditAction.LOGIN_SUCCEEDED,
        origin=Origin(actor=user, subject=user.email if user else "", ip=_ip(request)),
    )


@receiver(user_logged_out)
def on_logout(
    sender: object,  # noqa: ARG001
    request: HttpRequest | None = None,
    user: User | None = None,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Record a sign-out."""
    record_platform_event(
        action=AuditAction.LOGOUT,
        origin=Origin(actor=user, subject=user.email if user else "", ip=_ip(request)),
    )


@receiver(user_login_failed)
def on_login_failed(
    sender: object,  # noqa: ARG001
    credentials: dict[str, Any] | None = None,
    request: HttpRequest | None = None,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Record a rejected credential.

    The submitted address is stored as text, not as a foreign key: the interesting
    case is an address with no account behind it, and a FK cannot express that.
    """
    submitted = (credentials or {}).get("username") or (credentials or {}).get("email")
    record_platform_event(
        action=AuditAction.LOGIN_FAILED,
        origin=Origin(subject=str(submitted or ""), ip=_ip(request)),
    )


def _record_authenticator_change(
    action: str,
    request: HttpRequest | None,
    user: User | None,
    authenticator: object,
) -> None:
    record_platform_event(
        action=action,
        origin=Origin(actor=user, subject=user.email if user else "", ip=_ip(request)),
        metadata={"authenticator_type": getattr(authenticator, "type", "")},
    )


@receiver(authenticator_added)
def on_authenticator_added(
    sender: object,  # noqa: ARG001
    request: HttpRequest | None = None,
    user: User | None = None,
    authenticator: object = None,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Record MFA enrolment."""
    _record_authenticator_change(AuditAction.MFA_ENROLLED, request, user, authenticator)


@receiver(authenticator_removed)
def on_authenticator_removed(
    sender: object,  # noqa: ARG001
    request: HttpRequest | None = None,
    user: User | None = None,
    authenticator: object = None,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Record MFA removal."""
    _record_authenticator_change(AuditAction.MFA_REMOVED, request, user, authenticator)


@receiver(authenticator_reset)
def on_authenticator_reset(
    sender: object,  # noqa: ARG001
    request: HttpRequest | None = None,
    user: User | None = None,
    authenticator: object = None,
    **kwargs: Any,  # noqa: ANN401, ARG001
) -> None:
    """Record regenerated recovery codes."""
    _record_authenticator_change(AuditAction.MFA_RESET, request, user, authenticator)
