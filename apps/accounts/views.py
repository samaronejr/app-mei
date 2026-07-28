"""HTTP surface for the invitation flow.

Acceptance is mounted at the **platform** level rather than on a firm's subdomain, and
that is load-bearing rather than cosmetic: `TenantMiddleware` refuses an authenticated
request on a subdomain the account has no membership for, which is exactly the state
every invitee is in until the moment they redeem. Served from the subdomain, the
acceptance link would 403 for precisely the people it was sent to.
"""

from http import HTTPStatus

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.forms import InviteAcceptForm, InviteIssueForm
from apps.accounts.invites import (
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
from apps.tenants.models import Invite


class AuthenticatedRequest(HttpRequest):
    """An `HttpRequest` past `login_required`, so `user` is never anonymous."""

    user: User


REFUSAL_TEMPLATE = "accounts/invite_refused.html"
ACCEPT_TEMPLATE = "accounts/invite_accept.html"

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
def issue_invite_view(request: AuthenticatedRequest) -> HttpResponse:
    """Invite a colleague into the firm this request is scoped to."""
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
