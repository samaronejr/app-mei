"""HTTP surface for the invitation flow.

Acceptance is mounted at the **platform** level rather than on a firm's subdomain, and
that is load-bearing rather than cosmetic: `TenantMiddleware` refuses an authenticated
request on a subdomain the account has no membership for, which is exactly the state
every invitee is in until the moment they redeem. Served from the subdomain, the
acceptance link would 403 for precisely the people it was sent to.
"""

from http import HTTPStatus
from smtplib import SMTPException
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import sentry_sdk
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.forms import (
    InviteAcceptForm,
    InviteIssueForm,
    PortalInviteIssueForm,
)
from apps.accounts.invites import (
    INVITE_CAPABILITY,
    InviteAccountExistsError,
    InviteAlreadyAcceptedError,
    InviteEmailMismatchError,
    InviteError,
    InviteExpiredError,
    InviteHostMismatchError,
    InviteNotFoundError,
    InviteNotPermittedError,
    InviteNotRevocableError,
    InviteRevokedError,
    InviteScope,
    InviteScopeMismatchError,
    accept_invite,
    issue_client_invite,
    issue_invite,
    register_and_accept,
    resolve_invite,
    revoke_invite,
)
from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.authz.portfolio import visible_clients
from apps.authz.services import require_can
from apps.portal.middleware import PORTAL_URLCONF
from apps.tenants.models import Invite, Membership, Tenant, TenantRole
from apps.tenants.validators import PORTAL_HOST_SUFFIX


class AuthenticatedRequest(HttpRequest):
    """An `HttpRequest` past `login_required`, so `user` is never anonymous."""

    user: User


REFUSAL_TEMPLATE = "accounts/invite_refused.html"
ACCEPT_TEMPLATE = "accounts/invite_accept.html"
MEMBER_CONFIRM_TEMPLATE = "accounts/member_confirm.html"
INVITE_CONFIRM_TEMPLATE = "accounts/invite_confirm.html"
INVITATION_SEND_FAILURE = (
    "Não foi possível enviar o convite por e-mail. Nada foi criado; tente novamente."
)


class MemberLifecycleError(Exception):
    """A membership edit the firm's own rules will not allow.

    Not `PermissionDenied`: the caller holds `users.create`, and answering 403 would
    say the account may not do this at all, when what is wrong is the state of this
    particular row. The refusals below are conflicts, and each carries the sentence
    the reader is shown.
    """


_STATUS_BY_ERROR: dict[type[InviteError], HTTPStatus] = {
    InviteNotFoundError: HTTPStatus.NOT_FOUND,
    # Listed individually although both subclass InviteNotFoundError: the lookup below
    # is on `type(error)` exactly, so inheritance buys nothing here and an unlisted
    # subclass would quietly fall through to 400 — reporting a wrong-firm link as
    # malformed input rather than as no link at all.
    InviteHostMismatchError: HTTPStatus.NOT_FOUND,
    InviteScopeMismatchError: HTTPStatus.NOT_FOUND,
    InviteExpiredError: HTTPStatus.GONE,
    InviteAlreadyAcceptedError: HTTPStatus.GONE,
    # GONE, alongside its two siblings, because the three say the same thing to the
    # person holding the link: this credential existed and no longer works. Which of
    # the three it was is carried by the reason the page renders, not by the code.
    InviteRevokedError: HTTPStatus.GONE,
    InviteEmailMismatchError: HTTPStatus.FORBIDDEN,
    InviteAccountExistsError: HTTPStatus.CONFLICT,
}


def status_for_invite_error(error: InviteError) -> HTTPStatus:
    """Return the status an invitation refusal answers with, on either host.

    Public because the portal's acceptance route refuses the same domain errors and must
    answer them identically. A second copy of this mapping would let one host report a
    withdrawn link as gone and the other as merely malformed, and the invitee reading
    two different sentences about the same token has no way to know which is true.
    """
    return _STATUS_BY_ERROR.get(type(error), HTTPStatus.BAD_REQUEST)


def _refuse(request: HttpRequest, error: InviteError) -> HttpResponse:
    status = status_for_invite_error(error)
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
    try:
        _mail_invitation(form.cleaned_data["email"], tenant.name, raw_token)
    except (SMTPException, OSError):
        _mark_invitation_delivery_failure(form)
        return TemplateResponse(
            request,
            "accounts/team.html",
            _team_context(request, tenant, form),
        )
    return HttpResponseRedirect(reverse("invite-issued"))


def _mark_invitation_delivery_failure(
    form: InviteIssueForm | PortalInviteIssueForm,
) -> None:
    transaction.set_rollback(True)
    sentry_sdk.capture_exception()
    form.add_error(None, INVITATION_SEND_FAILURE)


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


def portal_accept_link(tenant_slug: str, raw_token: str) -> str:
    """Build the absolute acceptance URL on ONE firm's portal host.

    The host is derived rather than configured, because there is no second setting to
    derive it from: `PLATFORM_URL` names the deployment, and every portal lives at
    `<slug>-portal` under the same domain and port by construction — the reserved-slug
    validator exists to keep that mapping total.

    The path is reversed against the PORTAL urlconf explicitly. `reverse()` with no
    `urlconf` resolves against whatever tree the current request set, and this runs on a
    firm subdomain where that tree does not carry the name at all — so the default would
    raise `NoReverseMatch` on the one call site that exists.
    """
    parts = urlsplit(settings.PLATFORM_URL)
    host = f"{tenant_slug}{PORTAL_HOST_SUFFIX}.{parts.hostname or ''}"
    if parts.port:
        host = f"{host}:{parts.port}"
    path = reverse(
        "portal-invite-accept",
        args=[raw_token],
        urlconf=PORTAL_URLCONF,
    )
    return str(urlunsplit((parts.scheme, host, path, "", "")))


def _mail_portal_invitation(
    email: str,
    tenant_name: str,
    tenant_slug: str,
    raw_token: str,
) -> None:
    """Send the portal invitation down the same outbound path the firm invite uses.

    `send_mail` and nothing else, deliberately. A second transport — a task, a client, a
    provider SDK — would be a second place a bearer token can be logged, retried into a
    dead-letter queue, or delivered from an address the firm's domain does not
    authenticate. There is one way out of this product and this is it.
    """
    link = portal_accept_link(tenant_slug, raw_token)
    send_mail(
        subject=_("Acesso ao portal de %(firm)s") % {"firm": tenant_name},
        message=_(
            "Você foi convidado para acompanhar sua empresa no portal de "
            "%(firm)s.\n\nAcesse: %(link)s\n",
        )
        % {"firm": tenant_name, "link": link},
        from_email=None,
        recipient_list=[email],
    )


@require_POST
@login_required
@require_can(INVITE_CAPABILITY)
def portal_invite_issue_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Invite somebody into ONE client's portal, from the firm side.

    The client is selected out of `visible_clients()` and a miss raises `Http404`, which
    is `clients_detail_view`'s contract and is inherited rather than reinvented: the
    manager is tenant-scoped, so an unscoped lookup would already refuse another firm's
    client, but only the portfolio lens narrows a staff accountant to the clients they
    are actually assigned. A 404 also refuses to say whether the id names a real client,
    so the address bar cannot be walked to enumerate the firm's book.

    `require_can` sits above the tenant check here, inverting `clients_detail_view`'s
    order, and it is safe in this one direction only because the refusal it produces is
    UNIFORM: on the platform host no firm-side membership resolves, so `users.create` is
    NONE and every id — real, foreign or invented — answers 403 alike. Nothing is
    learned
    from a constant.

    No page, no confirmation of its own: the success redirect lands on `invite-issued`,
    which the firm invitation already uses and says the one thing that is true of both.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    client = (
        visible_clients(request.user, tenant_id=UUID(str(tenant.pk)))
        .filter(pk=pk)
        .first()
    )
    if client is None:
        raise Http404
    form = PortalInviteIssueForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            REFUSAL_TEMPLATE,
            {"reason": _("Dados inválidos.")},
            status=HTTPStatus.BAD_REQUEST,
        )
    try:
        _invite, raw_token = issue_client_invite(
            actor=request.user,
            tenant=tenant,
            client=client,
            email=form.cleaned_data["email"],
            role=form.cleaned_data["role"],
        )
    except InviteNotPermittedError as error:
        raise PermissionDenied(str(error)) from error
    try:
        _mail_portal_invitation(
            form.cleaned_data["email"],
            tenant.name,
            tenant.slug,
            raw_token,
        )
    except (SMTPException, OSError):
        from apps.clients.views import client_detail_context  # noqa: PLC0415

        context = client_detail_context(request, client, portal_invite_form=form)
        _mark_invitation_delivery_failure(form)
        return TemplateResponse(
            request,
            "clients/detail.html",
            context,
            status=HTTPStatus.OK,
        )
    return HttpResponseRedirect(reverse("invite-issued"))


def assert_firm_invite(*, invite: Invite) -> None:
    """Refuse an invitation that does not belong at the FIRM's acceptance door.

    The mirror of `assert_portal_invite`'s scope arm, and the symmetry is the whole
    content of it. That one turns a FIRM-side invitation away from `<slug>-portal`,
    because redeeming it there mints the firm-wide row
    `TenantMiddleware._grant_context` reads as proof of firm-side access, out of a flow
    whose premise is that it grants one client. This one turns a CLIENT-scoped
    invitation away from the platform host, because redeeming it here mints a portal
    seat out of a link that never named which firm's portal the seat is in — on the one
    host where there is no portal context to check it against.

    `resolve_invite` cannot make this call for either route. It is shared by both and
    finds an invitation by token digest alone, which is all an anonymous caller can be
    asked for; which door the token was presented at is a fact only the door holds.

    A named function rather than an `if` inside the view keeps the pre-render guard
    callable and testable. Commit `e908d89` recorded its location beside this route as
    the authority boundary; this hardening supersedes that decision. The view guard
    remains because a wrong-door GET renders before domain acceptance runs, while the
    required `InviteScope.FIRM` is revalidated after the row lock for future non-HTTP
    callers and against a check-to-use race. `_client_seat` already bound a minted row
    to `invite.client_id`, but it did not bind the caller's host or entry-point
    expectation. The new domain authority supplies that missing half.

    The sentence is `resolve_invite`'s own, character for character, and that is
    deliberate. `_refuse` prints `str(error)`, so on this host a distinct sentence
    would share the 404 and still disclose that the token names a real client-scoped
    invitation somewhere — precisely the fact the shared status exists to withhold,
    which is what `_STATUS_BY_ERROR`'s comment above is about. A wrong-scope link has
    to read as no link at all in the page as well as in the code, and
    `tests/accounts/test_invite_route_scope.py` compares the two rendered documents
    whole rather than taking this paragraph's word for it.
    """
    if invite.client_id is not None:
        msg = "No invitation matches that link."
        raise InviteScopeMismatchError(msg)


@require_http_methods(["GET", "POST"])
def accept_invite_view(request: HttpRequest, token: str) -> HttpResponse:
    """Redeem a FIRM-side invitation, binding it to the invited mailbox.

    Two gates before either branch below, and their ORDER is what they are worth.
    `resolve_invite` refuses an unknown, spent, withdrawn or lapsed token;
    `assert_firm_invite` refuses one whose seat is inside a client. Both sit ahead of
    the lock-and-accept, so a link turned away here is still redeemable at the door it
    was actually addressed to. The same refusal written after `register_and_accept`
    would answer 404 just as convincingly and spend the invitation on the way, leaving
    the person it was mailed to holding a credential they never got to use.
    """
    try:
        invite = resolve_invite(token)
        assert_firm_invite(invite=invite)
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
        accept_invite(
            invite=invite,
            user=user,
            expected_scope=InviteScope.FIRM,
        )
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
            expected_scope=InviteScope.FIRM,
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


def _team_context(
    request: AuthenticatedRequest,
    tenant: Tenant,
    form: InviteIssueForm,
) -> dict[str, object]:
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
    # revoked_at__isnull=True alongside accepted_at: a withdrawn invitation is not
    # pending, and leaving it in this list would offer a Revogar link for something
    # already stood down — and, worse, tell the firm a link is live when it is dead.
    pending = (
        Invite.objects.for_user(request.user)
        .filter(
            tenant=tenant,
            client__isnull=True,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
        )
        .order_by("email")
    )
    return {"memberships": memberships, "pending_invites": pending, "form": form}


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
    return render(
        request,
        "accounts/team.html",
        _team_context(request, tenant, InviteIssueForm()),
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


def _visible_invite(request: AuthenticatedRequest, pk: UUID) -> Invite:
    """Resolve one invitation the caller is actually entitled to see.

    The twin of `_visible_membership`, and it answers 404 for the same reason: a 403
    confirms the primary key names a real invitation somewhere on the platform, which
    is the fact a tenant boundary exists to withhold.

    This one carries more weight than its twin, because it is the ONLY thing scoping
    these rows. `tenants_invite` is in `NON_TENANT_TABLES`, so the database enforces
    nothing, and `tests/tenants/test_membership_scope_guard.py` scans for the
    membership manager rather than this one — so an unscoped lookup here trips no
    meta-test at all. It would simply let one firm withdraw another firm's
    invitations, in silence. `tests/accounts/test_invite_revocation.py` is what
    stands in for the guard that cannot see this.

    No `client` filter, unlike the membership twin — and this is the paragraph where
    the premise that used to excuse that stopped being true. W7 gave `Invite` a nullable
    `client` and widened `invite_role_is_firm_side` to admit a client role when it is
    set; `7d71ff6` then shipped the flow that ISSUES one. So client-scoped rows exist,
    `team_view`'s pending list filters on tenant and nothing else, and the Revogar link
    beside such a row lands here.

    Tenant is still the whole boundary, for a reason that is about the CAPABILITY rather
    than about which rows exist. Issuing a portal invitation and withdrawing one are
    gated identically — `portal_invite_issue_view` and `invite_revoke_view` both sit
    behind `users.create`, and `revoke_invite` re-checks it against the invitation's own
    tenant — so anybody who can reach a client-scoped row through this function could
    have created it in the first place, and narrowing the lookup would withhold nothing
    from them. What the client scope is owed here is a RECORD rather than a filter, and
    it gets one: `_invite_metadata` puts `client_id` on all three legs of an
    invitation's life, so the withdrawal names whose books the dead link would have
    opened. A view that LISTS portal invitations on their own screen has to answer this
    question again, because there a filter is what decides who reads what.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    scoped = Invite.objects.for_user(request.user).filter(tenant=tenant)
    try:
        return scoped.get(pk=pk)
    except Invite.DoesNotExist as missing:
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


def _render_invite_confirm(
    request: AuthenticatedRequest,
    invite: Invite,
    *,
    reason: str = "",
    status: HTTPStatus = HTTPStatus.OK,
) -> HttpResponse:
    return render(
        request,
        INVITE_CONFIRM_TEMPLATE,
        {"invite": invite, "reason": reason},
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_can(INVITE_CAPABILITY)
def invite_revoke_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Confirm, then perform, the withdrawal of an outstanding invitation.

    Guarded exactly as `member_deactivate_view` is, and shaped the same way: one route
    for both halves so the confirmation and the action cannot drift apart, and so a
    refusal re-renders the page the reader is already on with its cancel link intact.

    A confirmation step here and not on `member-reactivate`, because the two are not
    equally reversible. Reactivation is the undo of something the same button did;
    withdrawing an invitation destroys a credential that cannot be handed back — the
    raw token was never stored — so the remedy is issuing a new one and asking the
    invitee to use a different link.

    The refusal is a 409 rather than a 403, matching its sibling: `require_can` has
    already answered the authorization question, and what is left is the state of this
    row. Reporting that as a permission failure sends an administrator looking for a
    grant that is already there. The 410 in `_STATUS_BY_ERROR` is the other audience —
    the invitee holding a dead link — and is a different flow.
    """
    invite = _visible_invite(request, pk)
    if request.method == "GET":
        return _render_invite_confirm(request, invite)
    try:
        revoke_invite(actor=request.user, invite=invite)
    except InviteNotRevocableError as refused:
        return _render_invite_confirm(
            request,
            invite,
            reason=str(refused),
            status=HTTPStatus.CONFLICT,
        )
    messages.success(
        request,
        _("O convite para %(email)s foi revogado.") % {"email": invite.email},
    )
    return HttpResponseRedirect(reverse("team"))
