"""HTTP surface for the invitation flow.

Acceptance is mounted at the **platform** level rather than on a firm's subdomain, and
that is load-bearing rather than cosmetic: `TenantMiddleware` refuses an authenticated
request on a subdomain the account has no membership for, which is exactly the state
every invitee is in until the moment they redeem. Served from the subdomain, the
acceptance link would 403 for precisely the people it was sent to.
"""

from http import HTTPStatus
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.forms import InviteAcceptForm, InviteIssueForm
from apps.accounts.invites import (
    INVITE_CAPABILITY,
    InviteAccountExistsError,
    InviteAlreadyAcceptedError,
    InviteEmailMismatchError,
    InviteError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteNotPermittedError,
    accept_invite,
    issue_invite,
    register_and_accept,
    resolve_invite,
)
from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.services import require_can
from apps.tenants.models import Invite, Membership, TenantRole


class AuthenticatedRequest(HttpRequest):
    """An `HttpRequest` past `login_required`, so `user` is never anonymous."""

    user: User


REFUSAL_TEMPLATE = "accounts/invite_refused.html"
ACCEPT_TEMPLATE = "accounts/invite_accept.html"
MEMBER_CONFIRM_TEMPLATE = "accounts/member_confirm.html"


class MemberLifecycleError(Exception):
    """A membership edit the firm's own rules will not allow.

    Not `PermissionDenied`: the caller holds `users.create`, and answering 403 would
    say the account may not do this at all, when what is wrong is the state of this
    particular row. The refusals below are conflicts, and each carries the sentence
    the reader is shown.
    """


_STATUS_BY_ERROR: dict[type[InviteError], HTTPStatus] = {
    InviteNotFoundError: HTTPStatus.NOT_FOUND,
    InviteExpiredError: HTTPStatus.GONE,
    InviteAlreadyAcceptedError: HTTPStatus.GONE,
    InviteEmailMismatchError: HTTPStatus.FORBIDDEN,
    InviteAccountExistsError: HTTPStatus.CONFLICT,
}


def _refuse(request: HttpRequest, error: InviteError) -> HttpResponse:
    status = _STATUS_BY_ERROR.get(type(error), HTTPStatus.BAD_REQUEST)
    return render(
        request,
        REFUSAL_TEMPLATE,
        {"reason": str(error)},
        status=status,
    )


@require_POST
@login_required
@require_can(INVITE_CAPABILITY)
def issue_invite_view(request: AuthenticatedRequest) -> HttpResponse:
    """Invite a colleague into the firm this request is scoped to.

    Gated twice on purpose. The decorator guards the HTTP surface and records the
    refusal as a `PlatformEvent`; `issue_invite` re-checks so that a management
    command, a future API, or a test helper cannot reach issuance without the same
    capability. The interim `Membership.role` gate T-017 shipped with is gone from
    both, which is what the role-check guard test enforces.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    form = InviteIssueForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            REFUSAL_TEMPLATE,
            {"reason": _("Dados inválidos.")},
            status=HTTPStatus.BAD_REQUEST,
        )
    try:
        _invite, raw_token = issue_invite(
            actor=request.user,
            tenant=tenant,
            email=form.cleaned_data["email"],
            role=form.cleaned_data["role"],
        )
    except InviteNotPermittedError as error:
        raise PermissionDenied(str(error)) from error
    _mail_invitation(form.cleaned_data["email"], tenant.name, raw_token)
    return HttpResponseRedirect(reverse("invite-issued"))


def _mail_invitation(email: str, tenant_name: str, raw_token: str) -> None:
    link = f"{settings.PLATFORM_URL}{reverse('invite-accept', args=[raw_token])}"
    send_mail(
        subject=_("Convite para %(firm)s") % {"firm": tenant_name},
        message=_(
            "Você foi convidado para %(firm)s.\n\nAcesse: %(link)s\n",
        )
        % {"firm": tenant_name, "link": link},
        from_email=None,
        recipient_list=[email],
    )


@require_http_methods(["GET", "POST"])
def accept_invite_view(request: HttpRequest, token: str) -> HttpResponse:
    """Redeem an invitation, binding it to the invited mailbox."""
    try:
        invite = resolve_invite(token)
    except InviteError as error:
        return _refuse(request, error)

    # isinstance rather than `.is_authenticated`: AnonymousUser answers that
    # property too, and the two branches below need genuinely different types.
    user = request.user
    if isinstance(user, User):
        return _accept_as_signed_in_user(request, invite, user)
    return _accept_as_new_account(request, invite)


def _accept_as_signed_in_user(
    request: HttpRequest,
    invite: Invite,
    user: User,
) -> HttpResponse:
    """Attach the membership only if this session owns the invited address.

    A session belonging to anyone else is refused rather than quietly served: the
    membership is never attached to `request.user` just because they hold the link.
    """
    if request.method == "GET":
        return render(request, ACCEPT_TEMPLATE, {"invite": invite, "form": None})
    try:
        accept_invite(invite=invite, user=user)
    except InviteError as error:
        return _refuse(request, error)
    return HttpResponseRedirect(settings.LOGIN_REDIRECT_URL)


def _accept_as_new_account(request: HttpRequest, invite: Invite) -> HttpResponse:
    if request.method == "GET":
        return render(
            request,
            ACCEPT_TEMPLATE,
            {"invite": invite, "form": InviteAcceptForm()},
        )
    form = InviteAcceptForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            ACCEPT_TEMPLATE,
            {"invite": invite, "form": form},
            status=HTTPStatus.BAD_REQUEST,
        )
    try:
        user, _membership = register_and_accept(
            invite=invite,
            password=form.cleaned_data["password2"],
            full_name=form.cleaned_data["full_name"],
        )
    except InviteError as error:
        return _refuse(request, error)
    _sign_in(request, user)
    return HttpResponseRedirect(settings.LOGIN_REDIRECT_URL)


def _sign_in(request: HttpRequest, user: User) -> None:
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")


def invite_issued_view(request: HttpRequest) -> HttpResponse:
    """Confirm that the invitation was sent, without echoing the token."""
    return render(request, "accounts/invite_issued.html", {})


@require_http_methods(["GET"])
@login_required
@require_can(INVITE_CAPABILITY)
def team_view(request: AuthenticatedRequest) -> HttpResponse:
    """List who belongs to this firm, and offer the form that invites another.

    `issue_invite_view` is POST-only and had no page to be posted from, so the
    capability that guards it had no destination a navigation bar could offer. This
    is that destination.

    Both reads go through `for_user`, which is the only scoping these three
    policy-free tables have — `Membership` and `Invite` cannot carry a row-level
    policy because the tenant-resolving middleware must read memberships before
    `app.tenant_id` exists. The extra `tenant` filter narrows the caller's own firms
    down to the one this subdomain names.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    # The outer filter is separate from the one inside for_user: that narrows WHICH
    # FIRMS the caller belongs to, while this narrows which rows come back. When the
    # manager's model IS Membership the two are not the same, and without this a
    # portal user appears on the firm's team page.
    memberships = (
        Membership.objects.for_user(request.user)
        .filter(tenant=tenant, client__isnull=True)
        .select_related("user")
        .order_by("user__email")
    )
    pending = (
        Invite.objects.for_user(request.user)
        .filter(tenant=tenant, accepted_at__isnull=True)
        .order_by("email")
    )
    return render(
        request,
        "accounts/team.html",
        {
            "memberships": memberships,
            "pending_invites": pending,
            "form": InviteIssueForm(),
        },
    )


def _visible_membership(request: AuthenticatedRequest, pk: UUID) -> Membership:
    """Resolve one firm-side membership the caller is actually entitled to see.

    Answers 404 rather than 403 for everything outside that set, and the difference
    matters: 403 confirms the primary key names a real row, which is exactly the fact
    a tenant boundary exists to withhold. Two kinds of row are invisible here — a
    membership in another firm, and a PORTAL identity inside this one.

    The second is the one `for_user` alone does not catch. It scopes by tenant, and a
    portal row carries the firm's tenant id, so it passes. Only `client__isnull=True`
    keeps a client's login off the firm's team page.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    scoped = Membership.objects.for_user(request.user).filter(
        tenant=tenant,
        client__isnull=True,
    )
    try:
        return scoped.select_related("user").get(pk=pk)
    except Membership.DoesNotExist as missing:
        raise Http404 from missing


@transaction.atomic
def _deactivate(*, actor: User, membership: Membership) -> None:
    """Revoke a colleague's access to this firm, or refuse and say why.

    The owner check reads the candidates **under a row lock taken before it counts**,
    and that ordering is the whole point. `select_for_update` then `len` is not the
    same as `count` then write: two requests removing two different owners both read
    two, both pass an unlocked check, and the firm ends with nobody who can restore
    one — `users.create` is NONE for `staff_accountant`, so no remaining account can
    promote anybody. Measured, not assumed: the unlocked shape leaves zero owners.

    `apps/accounts/invites.py:142-149` takes the same lock for the same reason.

    Owner-ness is decided by SET MEMBERSHIP rather than by reading a role off the
    instance. That keeps the rule as one ORM predicate the database evaluates while
    holding the lock — a Python-side comparison would be answering from a row read
    before the lock existed, which is the bug this function is built to avoid.
    """
    if membership.user_id == actor.pk:
        msg = _(
            "Você não pode desativar o seu próprio acesso. Peça a outra pessoa "
            "que administra a equipe para fazer isso.",
        )
        raise MemberLifecycleError(msg)

    owners = list(
        Membership.objects.for_user(actor)
        .select_for_update()
        .filter(
            tenant_id=membership.tenant_id,
            client__isnull=True,
            role=TenantRole.OWNER,
            is_active=True,
        )
        .order_by("pk")
        .values_list("pk", flat=True),
    )
    if membership.pk in owners and len(owners) <= 1:
        msg = _(
            "Este é o último proprietário ativo do escritório. Promova outra "
            "pessoa a proprietária antes de desativar esta.",
        )
        raise MemberLifecycleError(msg)

    membership.is_active = False
    membership.save(update_fields=["is_active"])
    record_event(
        action=AuditAction.MEMBERSHIP_DEACTIVATED,
        tenant_id=membership.tenant_id,
        actor=actor,
        obj=ObjectRef(type="membership", id=str(membership.pk)),
        metadata={"role": membership.role},
    )


@transaction.atomic
def _reactivate(*, actor: User, membership: Membership) -> None:
    """Give a previously revoked colleague their access back."""
    membership.is_active = True
    membership.save(update_fields=["is_active"])
    record_event(
        action=AuditAction.MEMBERSHIP_REACTIVATED,
        tenant_id=membership.tenant_id,
        actor=actor,
        obj=ObjectRef(type="membership", id=str(membership.pk)),
        metadata={"role": membership.role},
    )


def _render_member_confirm(
    request: AuthenticatedRequest,
    membership: Membership,
    *,
    reason: str = "",
    status: HTTPStatus = HTTPStatus.OK,
) -> HttpResponse:
    return render(
        request,
        MEMBER_CONFIRM_TEMPLATE,
        {"membership": membership, "reason": reason},
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_can(INVITE_CAPABILITY)
def member_deactivate_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Confirm, then perform, the removal of a colleague's access.

    One route for both halves so the confirmation and the action cannot drift apart,
    and so a refusal re-renders the page the reader is already on — with its cancel
    link intact — instead of stranding them on an error screen.

    The refusal is a 409 rather than a 403. `require_can` has already answered the
    authorization question; what is left is the state of this row, and reporting that
    as a permission failure would send an administrator looking for a missing grant.
    """
    membership = _visible_membership(request, pk)
    if request.method == "GET":
        return _render_member_confirm(request, membership)
    try:
        _deactivate(actor=request.user, membership=membership)
    except MemberLifecycleError as refused:
        return _render_member_confirm(
            request,
            membership,
            reason=str(refused),
            status=HTTPStatus.CONFLICT,
        )
    messages.success(
        request,
        _("O acesso de %(email)s foi desativado.") % {"email": membership.user.email},
    )
    return HttpResponseRedirect(reverse("team"))


@require_POST
@login_required
@require_can(INVITE_CAPABILITY)
def member_reactivate_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Restore access that was revoked, with no confirmation step.

    Deliberately asymmetric with deactivation. Granting access back is reversible by
    the same button that undid it, so a confirmation page here would be a click that
    protects nothing — and the row is still visible on the team page either way.
    """
    membership = _visible_membership(request, pk)
    _reactivate(actor=request.user, membership=membership)
    messages.success(
        request,
        _("O acesso de %(email)s foi reativado.") % {"email": membership.user.email},
    )
    return HttpResponseRedirect(reverse("team"))
