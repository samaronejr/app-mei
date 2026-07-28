"""The one place that answers "may this account do this?".

Everything else asks here. The reason for a single entry point rather than a decorator
per level is the matrix itself: three of its seven levels — `limited`, `own`, and the
two channel levels — cannot be decided from the role alone. `limited` needs the object
to know whether the account is attached to it; `own` needs the object to know who acted.
A decorator that only saw the role would have to guess, and guessing in the permissive
direction is how a staff accountant ends up reading a client they were never assigned.

`can()` answers one narrow question: **may this account perform the action outright?**
`view_confirm`, `initiate_approve` and `ticket_only` therefore answer `False` — the
holder may take part in the workflow through a specific channel, but cannot perform it.
Those three are readable through `resolve_level`, which is what a screen offering the
restricted variant should call. Collapsing them into `can()` would be the permissive
guess this module exists to avoid.
"""

from collections.abc import Callable, Iterable
from functools import wraps
from typing import Concatenate, ParamSpec, TypeVar
from uuid import UUID

from django.apps import apps as django_apps
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.http.response import HttpResponseBase

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import Origin, record_platform_event
from apps.authz.models import Capability, GrantLevel, RoleGrant
from apps.core.tenancy import current_tenant_id

_P = ParamSpec("_P")
_ResponseT = TypeVar("_ResponseT", bound=HttpResponseBase)
_RequestT = TypeVar("_RequestT", bound=HttpRequest)

Actor = User | AnonymousUser


class UnknownCapability(LookupError):  # noqa: N818
    """Raised when a call site names a capability the matrix does not define.

    Deliberately an exception rather than a `False`. A typo in a slug would otherwise
    read as "this account may not do that", which is indistinguishable from a working
    denial and would hide a permanently broken gate behind a plausible 403.
    """


def resolve_level(
    user: Actor,
    action: str,
    *,
    tenant_id: UUID | None = None,
) -> str:
    """Return the raw matrix level for this account, before object refinement.

    Resolution order, and the order matters: superuser short-circuit, then the active
    membership's role, then the grant. The short-circuit comes first because the
    report's *platform admin* column has no membership to look up — `Membership.role`
    offers only the three firm-side roles — so a membership-first order would deny the
    platform administrator every capability the matrix grants them.
    """
    capability = _capability(action)
    if not user.is_authenticated:
        return GrantLevel.NONE
    if user.is_superuser:
        return GrantLevel.FULL
    role = role_of(user, tenant_id)
    if role is None:
        return GrantLevel.NONE
    grant = RoleGrant.objects.filter(role=role, capability=capability).first()
    return grant.level if grant is not None else GrantLevel.NONE


def can(
    user: Actor,
    action: str,
    obj: object | None = None,
    *,
    tenant_id: UUID | None = None,
) -> bool:
    """Report whether this account may perform this action, on this object.

    Four parameters rather than three: `user`, `action` and `obj` are the triple the
    design names, and `tenant_id` is a keyword-only escape for the callers that run
    outside a resolved request — a management command, or a domain function handed an
    explicit tenant. Folding them into a value object would make every gate in the
    codebase build a struct to ask a yes/no question.
    """
    level = resolve_level(user, action, tenant_id=tenant_id)
    if level == GrantLevel.FULL:
        return True
    # The two object-resolved levels are unreachable for an anonymous caller, since
    # resolve_level answers NONE for one. Narrowing here rather than asserting it
    # keeps that fact checked by the type system instead of remembered.
    if not isinstance(user, User):
        return False
    if level == GrantLevel.LIMITED:
        return _is_attached_to(user, obj)
    if level == GrantLevel.OWN:
        return _is_own_record(user, obj)
    return False


def granted_levels(
    user: Actor,
    actions: Iterable[str],
    *,
    tenant_id: UUID | None = None,
) -> dict[str, str]:
    """Resolve several capabilities in one round trip, with `resolve_level`'s answers.

    A navigation bar asks about every item it might draw, and `resolve_level` costs
    two queries per question — so a six-item nav is twelve queries on every page.
    This is the same two reads for the whole set.

    It is not a second policy source: the resolution order, the superuser
    short-circuit and the missing-grant default are the same, and
    `test_bulk_resolution_agrees_with_resolve_level` walks every capability and every
    role to prove the two cannot drift.
    """
    slugs = tuple(dict.fromkeys(actions))
    known = set(
        Capability.objects.filter(slug__in=slugs).values_list("slug", flat=True),
    )
    unknown = [slug for slug in slugs if slug not in known]
    if unknown:
        msg = (
            f"{unknown} are not capabilities in the permission matrix. Returning "
            f"NONE here would hide a permanently broken gate behind a plausible 403."
        )
        raise UnknownCapability(msg)
    if not user.is_authenticated:
        return dict.fromkeys(slugs, GrantLevel.NONE)
    if user.is_superuser:
        return dict.fromkeys(slugs, GrantLevel.FULL)
    role = role_of(user, tenant_id)
    if role is None:
        return dict.fromkeys(slugs, GrantLevel.NONE)
    resolved = dict(
        RoleGrant.objects.filter(
            role=role,
            capability__slug__in=slugs,
        ).values_list("capability__slug", "level"),
    )
    return {slug: resolved.get(slug, GrantLevel.NONE) for slug in slugs}


def require_can(
    action: str,
) -> Callable[
    [Callable[Concatenate[_RequestT, _P], _ResponseT]],
    Callable[Concatenate[_RequestT, _P], _ResponseT],
]:
    """Gate a view on one capability, recording the refusal where it survives.

    The denial is written as a `PlatformEvent`, not an `Event`: `ATOMIC_REQUESTS`
    makes the view a savepoint, and raising `PermissionDenied` rolls it back — so a
    tenant-scoped audit row written here would be erased along with the request it
    was recording. Refusals are exactly the rows an investigation wants.
    """

    def decorate(
        view: Callable[Concatenate[_RequestT, _P], _ResponseT],
    ) -> Callable[Concatenate[_RequestT, _P], _ResponseT]:
        @wraps(view)
        def guarded(
            request: _RequestT,
            /,
            *args: _P.args,
            **kwargs: _P.kwargs,
        ) -> _ResponseT:
            if not can(request.user, action):
                _record_refusal(request, action)
                msg = f"This account may not {action}."
                raise PermissionDenied(msg)
            return view(request, *args, **kwargs)

        return guarded

    return decorate


def _record_refusal(request: HttpRequest, action: str) -> None:
    tenant = getattr(request, "tenant", None)
    record_platform_event(
        action=AuditAction.DENIED,
        origin=Origin(actor=request.user, subject=action),
        tenant_id=tenant.pk if tenant is not None else None,
        metadata={"capability": action, "path": request.path},
    )


def _capability(action: str) -> Capability:
    capability = Capability.objects.filter(slug=action).first()
    if capability is None:
        msg = (
            f"{action!r} is not a capability in the permission matrix. Returning "
            f"False here would hide a permanently broken gate behind a plausible 403."
        )
        raise UnknownCapability(msg)
    return capability


def role_of(user: User, tenant_id: UUID | None = None) -> str | None:
    """Return this account's active role in the named firm, or None if it has none.

    Public because `apps.authz.portfolio` needs it, and because the role-check guard
    requires every role comparison in the codebase to live inside this package — so
    the read that feeds one belongs here too rather than being re-implemented next to
    the comparison.
    """
    resolved = tenant_id or current_tenant_id.get()
    if resolved is None:
        return None
    membership_model = django_apps.get_model("tenants", "Membership")
    # PLATFORM_QUERY_OK: keyed on the actor and one explicit tenant. This query IS
    # the authorization check, so routing it through for_user would be circular.
    membership = membership_model.objects.filter(
        user=user,
        tenant_id=resolved,
        is_active=True,
    ).first()
    return str(membership.role) if membership is not None else None


def _is_attached_to(user: User, obj: object | None) -> bool:
    """Resolve `limited`: report whether this account is assigned to that client."""
    if obj is None:
        return False
    client_id = _client_id_of(obj)
    if client_id is None:
        return False
    assignment_model = django_apps.get_model("clients", "ClientAssignment")
    return bool(
        assignment_model.objects.filter(user=user, client_id=client_id).exists(),
    )


def _client_id_of(obj: object) -> object | None:
    company_model = django_apps.get_model("clients", "ClientCompany")
    if isinstance(obj, company_model):
        primary_key: object | None = obj.pk
        return primary_key
    client_id: object | None = getattr(obj, "client_id", None)
    return client_id


def _is_own_record(user: User, obj: object | None) -> bool:
    """Resolve `own`: report whether this account produced the record."""
    if obj is None:
        return False
    if obj is user:
        return True
    for attribute in ("actor_id", "user_id"):
        owner = getattr(obj, attribute, None)
        if owner is not None:
            return bool(owner == user.pk)
    return False


__all__ = [
    "Actor",
    "UnknownCapability",
    "can",
    "granted_levels",
    "require_can",
    "resolve_level",
    "role_of",
]
