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

**Both screens render on the ENTRANCE shell, and neither may reach the firm one.**
`templates/accounts/invite_accept.html` is the firm-side twin of the acceptance page
and cannot be reused here at all: it extends the firm layout, whose footer reverses
`dsr-submit`, a name the portal urlconf does not carry, so every render on this host
would be a `NoReverseMatch` 500. `templates/allauth/layouts/base.html` was made
standalone and dual-host for exactly this, and it is the same shell the enrolment
screen this flow hands over to already stands on.

**The templates are handed scalars, never the invitation.** This module runs with no
tenant context, so a lazy fetch of `invite.client` inside a render would raise
`ClientCompany.DoesNotExist` on a page that had already begun answering 200. The two
facts either page states — the invited address and the firm's name — are resolved here
and passed down, and the templates follow no relation.
"""

from http import HTTPStatus

from django.contrib.auth import login
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.forms import InviteAcceptForm
from apps.accounts.invites import (
    InviteAccountExistsError,
    InviteAlreadyAcceptedError,
    InviteEmailMismatchError,
    InviteError,
    InviteExpiredError,
    InviteHostMismatchError,
    InviteNotFoundError,
    InviteRevokedError,
    InviteScopeMismatchError,
    accept_invite,
    assert_portal_invite,
    register_and_accept,
    resolve_invite,
)
from apps.accounts.models import User
from apps.accounts.views import status_for_invite_error
from apps.portal.hosts import portal_slug_from_host
from apps.tenants.models import Invite

ACCEPT_TEMPLATE = "accounts/portal_invite_accept.html"
REFUSAL_TEMPLATE = "accounts/portal_invite_refused.html"


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
        return _refused(request, error)

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
        return _refused(request, error)
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
        return _refused(request, error)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # Their own company, and from here `MFAEnforcementMiddleware` takes over: the seat
    # they just accepted is an active Membership, so the very next request diverts them
    # to enrolment before any page renders. `tests/ui/test_mfa_pages.py` follows that
    # hand-off hop by hop from the mailed link to the day-one landing page, and
    # `tests/ui/test_portal_invite_pages.py` pins the two screens it passes through.
    return HttpResponseRedirect(reverse("portal-home"))


def _reason_for(error: InviteError) -> str:
    """Return the pt-BR sentence the invitee reads, chosen by the error's TYPE.

    Not `str(error)`, and the difference is the whole point of this function. The domain
    writes its messages in English for a maintainer reading a traceback, the firm-side
    refusal page prints them verbatim, and `tests/accounts/test_invites.py` asserts on
    them — so they may not be translated where they are raised. The reader of the portal
    refusal page is a MEI owner on a phone who did nothing wrong except open a link a
    day too late, and English is not a sentence that helps them.

    Keyed on `type(error)` exactly rather than by `isinstance`, the same way
    `_STATUS_BY_ERROR` is keyed, so a subclass has to be listed in its own right and
    cannot inherit a sentence that is subtly about a different failure. The default
    exists for the same reason a default status does: an unlisted error must still
    produce a page in the reader's language rather than a blank announcement.

    Every line names what to DO next where there is anything to do. "Expired" and
    "withdrawn" both send the reader to the firm; "already used" sends them to sign in
    instead, because their account exists and the link has served its purpose.
    """
    sentences: dict[type[InviteError], str] = {
        InviteNotFoundError: _(
            "Este link não corresponde a nenhum convite deste escritório. Confira se "
            "você abriu o endereço completo, exatamente como está no e-mail.",
        ),
        InviteHostMismatchError: _(
            "Este link não corresponde a nenhum convite deste escritório. Confira se "
            "você abriu o endereço completo, exatamente como está no e-mail.",
        ),
        InviteScopeMismatchError: _(
            "Este link não corresponde a nenhum convite deste escritório. Confira se "
            "você abriu o endereço completo, exatamente como está no e-mail.",
        ),
        InviteExpiredError: _(
            "O prazo deste convite terminou. Peça um novo ao seu escritório de "
            "contabilidade.",
        ),
        InviteRevokedError: _(
            "Este convite foi cancelado pelo escritório. Fale com quem cuida da sua "
            "empresa lá para receber um novo.",
        ),
        InviteAlreadyAcceptedError: _(
            "Este convite já foi aceito e cada link vale uma única vez. Se o acesso é "
            "seu, entre com o seu e-mail e a senha que você cadastrou.",
        ),
        InviteEmailMismatchError: _(
            "Este convite foi enviado para outro endereço de e-mail. Entre com o "
            "endereço que recebeu o convite, ou peça um novo ao seu escritório.",
        ),
        InviteAccountExistsError: _(
            "Já existe uma conta com este endereço de e-mail. Entre com ela para "
            "aceitar o convite.",
        ),
    }
    return sentences.get(
        type(error),
        _("Não foi possível usar este convite. Peça um novo ao seu escritório."),
    )


def _refused(request: HttpRequest, error: InviteError) -> HttpResponse:
    """Answer a dead link with a page in the reader's language and the twin's status.

    The STATUS is the part that carries the meaning across both hosts and the part this
    page does not move: 404 for a link that names nothing here, 410 for one that existed
    and no longer works, 403 for a session that is not the invitee's, 409 for an address
    that already has an account. `status_for_invite_error` is imported rather than
    copied so the two hosts cannot start answering differently about the same token.
    """
    return render(
        request,
        REFUSAL_TEMPLATE,
        {"reason": _reason_for(error)},
        status=status_for_invite_error(error),
    )


def _accept_page(
    request: HttpRequest,
    invite: Invite,
    *,
    form: InviteAcceptForm | None,
    status: HTTPStatus = HTTPStatus.OK,
) -> HttpResponse:
    """Render the acceptance screen on the entrance shell, from scalars only.

    `invite.email` and `invite.tenant.name` are resolved HERE and passed down as plain
    strings. Both are already in memory by the time this runs — `resolve_invite`
    fetched the invitation and `assert_portal_invite` fetched the tenant to compare its
    slug — so this costs nothing, and it keeps the template free of a lazy fetch it
    would perform under no tenant context at all. `invite.client` is never reached for
    the same reason `_client_seat` does not reach it.

    `form` is None for somebody already signed in, who only confirms; the template's
    field loop and error summary render nothing in that case and the submit control is
    the same either way, because the action is.
    """
    return render(
        request,
        ACCEPT_TEMPLATE,
        {
            "invited_email": invite.email,
            "firm_name": invite.tenant.name,
            "form": form,
        },
        status=status,
    )
