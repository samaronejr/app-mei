"""Redeeming a portal invitation, on the portal host, before the invitee has a seat.

**Why this route is here and not beside its firm-side twin.** `apps/accounts/views.py`
mounts acceptance at the PLATFORM level because `TenantMiddleware` refuses an
authenticated request on a subdomain the account has no membership for — which is every
invitee's state until the moment they redeem. The portal has the same problem with a
different door: `PortalMiddleware._grant_context` refuses an authenticated account with
no CLIENT membership in this firm. The platform host cannot serve this flow either,
because the link has to name the firm whose portal the seat is in, and the platform host
names none. So the route lives on the portal urlconf, and
`PortalMiddleware.UNCONFINED_PREFIXES` is what keeps the membership gate and the
database role off it — see the constant for both failure modes.

**This module runs as `app_runtime` with both GUCs empty, and every line depends on
it.** It reads `tenants_invite` and writes `tenants_membership`, `accounts_user` and
`account_emailaddress`; `app_portal` holds SELECT on none of the four and INSERT on none
of them either. `apps/portal/views.py` documents the opposite contract for the four
pages that DO run under the role, and nothing in this file may be moved there.

**No template, and that is this wave's deliberate seam.** Todo 30 owns the portal accept
screen and renders it on the entrance shell. Two shapes were available in the meantime
and both are worse: a portal template here would be a second accept page for that todo
to delete, and reusing `templates/accounts/invite_accept.html` is not possible at all —
it extends the firm layout, whose footer reverses `dsr-submit`, a name the portal
urlconf does not carry, so every render on this host would be a `NoReverseMatch` 500.
`apps/portal/urls.py` already documents the same replace-the-body-in-place arrangement
for three other portal destinations.
"""

from http import HTTPStatus

from django.contrib.auth import login
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.urls import reverse
from django.utils.html import escape
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.forms import InviteAcceptForm
from apps.accounts.invites import (
    InviteError,
    accept_invite,
    assert_portal_invite,
    register_and_accept,
    resolve_invite,
)
from apps.accounts.models import User
from apps.accounts.views import status_for_invite_error
from apps.portal.hosts import portal_slug_from_host
from apps.tenants.models import Invite


@require_http_methods(["GET", "POST"])
def portal_invite_accept(request: HttpRequest, token: str) -> HttpResponse:
    """Redeem a portal invitation, binding it to the invited mailbox and to this firm.

    Three gates, in this order, and the order is what each of them is worth.
    `resolve_invite` refuses an unknown, spent, withdrawn or lapsed token;
    `assert_portal_invite` refuses one belonging to another firm or to the firm side;
    and `accept_invite` refuses a session that has not proved control of the invited
    address. The last of the three lives in the domain rather than here, so a management
    command or a future API cannot reach acceptance without it.
    """
    try:
        invite = resolve_invite(token)
        assert_portal_invite(
            invite=invite,
            firm_slug=portal_slug_from_host(request.get_host()),
        )
    except InviteError as error:
        return _refused(error)

    # isinstance rather than `.is_authenticated`: AnonymousUser answers that property
    # too, and the two branches below need genuinely different types.
    user = request.user
    if isinstance(user, User):
        return _accept_as_signed_in(request, invite, user)
    return _accept_as_new_account(request, invite)


def _accept_as_signed_in(
    request: HttpRequest,
    invite: Invite,
    user: User,
) -> HttpResponse:
    """Attach the client seat only if this session owns the invited address.

    The membership is never quietly attached to whoever holds the link. This is also the
    branch a firm-side accountant arrives on when they are given a seat in a client they
    keep books for: `accept_invite` ADDS a second membership rather than converting the
    one they already hold, because the firm door and the portal door read different
    rows.
    """
    if request.method == "GET":
        return _accept_page(request, invite, form=None)
    try:
        accept_invite(invite=invite, user=user)
    except InviteError as error:
        return _refused(error)
    return HttpResponseRedirect(reverse("portal-home"))


def _accept_as_new_account(request: HttpRequest, invite: Invite) -> HttpResponse:
    if request.method == "GET":
        return _accept_page(request, invite, form=InviteAcceptForm())
    form = InviteAcceptForm(request.POST)
    if not form.is_valid():
        return _accept_page(
            request,
            invite,
            form=form,
            status=HTTPStatus.BAD_REQUEST,
        )
    try:
        user, _membership = register_and_accept(
            invite=invite,
            password=form.cleaned_data["password2"],
            full_name=form.cleaned_data["full_name"],
        )
    except InviteError as error:
        return _refused(error)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # Their own company, and from here `MFAEnforcementMiddleware` takes over: the seat
    # they just accepted is an active Membership, so the very next request diverts them
    # to enrolment before any page renders. Todo 30 makes that hand-off a walkthrough.
    return HttpResponseRedirect(reverse("portal-home"))


def _refused(error: InviteError) -> HttpResponse:
    """Answer a dead link with its reason and the status its firm-side twin uses.

    `text/plain`, because this wave ships no page. The STATUS is the part that carries
    the meaning and the part todo 30 must not move: 404 for a link that names nothing
    here, 410 for one that existed and no longer works, 403 for a session that is not
    the invitee's. `status_for_invite_error` is imported rather than copied so the two
    hosts cannot start answering differently about the same token.
    """
    return HttpResponse(
        str(error),
        status=status_for_invite_error(error),
        content_type="text/plain; charset=utf-8",
    )


def _accept_page(
    request: HttpRequest,
    invite: Invite,
    *,
    form: InviteAcceptForm | None,
    status: HTTPStatus = HTTPStatus.OK,
) -> HttpResponse:
    """Render the unstyled placeholder standing in for todo 30's accept screen.

    Built here rather than from a template on purpose — the module docstring gives both
    reasons. What this must get right is the FLOW, and it does: the invited address is
    shown so the reader can recognise whether the link is theirs before typing anything,
    the CSRF token is minted from Django's own generator, and the form is the same
    `InviteAcceptForm` the firm-side flow validates against, so the fields cannot drift
    apart from the ones `register_and_accept` consumes.

    `form` is None for somebody already signed in, who only confirms; the submit control
    is the same either way, because the action is.
    """
    fields = form.as_p() if form is not None else ""
    title = escape(str(_("Aceitar convite")))
    body = (
        '<!doctype html><html lang="pt-br"><head><meta charset="utf-8">'
        f"<title>{title}</title></head><body>"
        f"<h1>{title}</h1>"
        f"<p>{escape(invite.email)}</p>"
        '<form method="post">'
        '<input type="hidden" name="csrfmiddlewaretoken" '
        f'value="{escape(get_token(request))}">'
        f"{fields}"
        f'<button type="submit">{escape(str(_("Aceitar")))}</button>'
        "</form></body></html>"
    )
    return HttpResponse(body, status=status)
