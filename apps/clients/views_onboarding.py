"""In-product onboarding screens (AXR1-007): create, edit and set a client up.

Every route here is behind the `import_r1` flag (404 while off), resolves the firm
before the capability (the `require_tenant` ordering documented in `views.py`), and
loads the client through `maintenance.load_client` — the actor's portfolio, 404 for
anything outside it. The domain services repeat that load under a row lock and
re-check the capability; the decorators here are the first layer, not the only one.

Status mapping (plan S7): a form error answers 422 on the same page, a stale version
answers 409 with a reload prompt, a missing capability 403.
"""

import logging
from functools import partial
from http import HTTPStatus
from smtplib import SMTPException
from typing import Any, Final
from uuid import UUID

import sentry_sdk
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.forms import Form
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.forms import portal_role_choices
from apps.accounts.invites import InviteNotPermittedError
from apps.accounts.models import User
from apps.accounts.views import AuthenticatedRequest
from apps.authz.services import can, require_can
from apps.clients.forms import (
    ASSIGNMENT_ROLE_CHOICES,
    MEI_CATEGORY_CHOICES,
    REGIME_CHOICES,
    SERVICE_CHOICES,
    AssignmentForm,
    ClientCreateForm,
    ClientEditForm,
    ContactForm,
    ContactInviteForm,
    EngagementForm,
    TaxProfileForm,
)
from apps.clients.maintenance import (
    ASSIGN_CAPABILITY,
    MANAGE_CAPABILITY,
    ClientData,
    ConflictError,
    ContactData,
    EngagementData,
    add_assignment,
    assignment_state,
    create_client,
    invite_contact,
    load_client,
    load_contact,
    remove_assignment,
    save_contact,
    save_engagement,
    set_tax_profile,
    update_client,
    withdraw_undelivered_invite,
)
from apps.clients.models import (
    ClientAssignment,
    ClientCompany,
    ClientContact,
    Engagement,
    ProfileConfidence,
    TaxProfile,
)
from apps.clients.views import ONBOARDING_FLAG, require_tenant
from apps.core.flags import require_feature, tenant_has
from apps.tenants.models import Invite, Membership, Tenant

FLAG: Final = ONBOARDING_FLAG
# pt-BR labels resolved in Python, so no template reaches for get_*_display.
SERVICE_LABELS: Final[dict[str, Any]] = {str(k): v for k, v in SERVICE_CHOICES}
REGIME_LABELS: Final[dict[str, Any]] = {str(k): v for k, v in REGIME_CHOICES}
CATEGORY_LABELS: Final[dict[str, Any]] = {str(k): v for k, v in MEI_CATEGORY_CHOICES}
# Where a regime claim came from, said plainly (invariant 8, S5.7): a value derived
# from the legacy MEI flag is never presented as a verified fact.
ORIGIN_LABELS: Final[dict[str, Any]] = {
    str(ProfileConfidence.STAFF_RECORDED): gettext_lazy("Registrado pela equipe"),
    str(ProfileConfidence.ASSERTED_BY_LEGACY_FLAG): gettext_lazy(
        "Deduzido do cadastro antigo (não verificado)",
    ),
}
ASSIGNMENT_LABELS: Final[dict[str, Any]] = {
    str(k): v for k, v in ASSIGNMENT_ROLE_CHOICES
}
SETUP_TEMPLATE: Final = "clients/setup.html"
CONFLICT_MESSAGE: Final = (
    "Este registro foi alterado por outra pessoa depois que você abriu a página. "
    "Nada foi salvo. Recarregue para ver a versão atual e tente de novo."
)
INVITE_SEND_FAILURE: Final = (
    "Não foi possível enviar o convite por e-mail. Nenhum convite ficou ativo; "
    "tente novamente em alguns minutos."
)
# The send was refused AND the automatic withdrawal failed: the invitation may still
# be live although nobody received it. Say exactly that, and point at the recovery.
INVITE_UNRESOLVED_NOTICE: Final = (
    "Não foi possível enviar o convite por e-mail, e ele não pôde ser cancelado "
    'automaticamente: pode continuar ativo sem ter sido recebido. Use "Cancelar '
    'convite pendente" neste contato e depois convide de novo.'
)
# S5.7: an SMTP acceptance is not a delivery, and never a reading.
INVITE_SENT_NOTICE: Final = (
    "Convite aceito pelo servidor de e-mail. Isso não confirma a leitura."
)
# T11: with `cycles_r1`, the invitation is queued on the outbox in the issuing
# transaction and mailed by the dispatcher; nothing has been sent yet when the page
# says so, and the Comunicação queue shows what the SMTP server later answered.
INVITE_QUEUED_NOTICE: Final = (
    "Convite na fila de envio. Acompanhe o envio na fila de comunicação; um aceite "
    "do servidor de e-mail não confirma a leitura."
)
INVITE_QUEUED: Final = "na-fila"
INVITE_SENT: Final = "enviado"
INVITE_FAILED: Final = "falhou"
INVITE_UNRESOLVED: Final = "pendente"

logger = logging.getLogger(__name__)


def _tenant(request: AuthenticatedRequest) -> Tenant:
    # `require_tenant` has already refused a request without one.
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    resolved: Tenant = tenant
    return resolved


def _tenant_id(request: AuthenticatedRequest) -> UUID:
    return UUID(str(_tenant(request).pk))


def _apply_errors(form: Form, error: ValidationError) -> None:
    if hasattr(error, "error_dict"):
        for field, messages in error.message_dict.items():
            target = field if field in form.fields else None
            for message in messages:
                form.add_error(target, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _team(tenant_id: UUID) -> list[tuple[str, str]]:
    # PLATFORM_QUERY_OK: the firm's own roster, keyed on the tenant this request
    # resolved; firm-side seats only (client IS NULL), active only.
    memberships = Membership.objects.filter(
        tenant_id=tenant_id,
        is_active=True,
        client__isnull=True,
    ).select_related("user")
    return sorted(
        ((str(m.user_id), m.user.full_name or m.user.email) for m in memberships),
        key=lambda pair: (pair[1].lower(), pair[0]),
    )


def _setup_context(
    request: AuthenticatedRequest,
    client: ClientCompany,
    **forms: Form,
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    team = _team(tenant_id)
    team_names = dict(team)
    engagements = list(
        Engagement.objects.filter(client=client).order_by("-starts_on", "pk"),
    )
    profiles = list(
        TaxProfile.objects.filter(client=client, superseded_at__isnull=True).order_by(
            "-valid_from",
            "pk",
        ),
    )
    contacts = list(
        ClientContact.objects.filter(client=client).order_by("-active", "name", "pk"),
    )
    assignments = list(
        ClientAssignment.objects.filter(client=client).order_by("created_at", "pk"),
    )
    # Scoped three ways, like the detail page's list: for_user, this firm, this
    # client (tenants_invite carries no row-level policy).
    pending = Invite.objects.for_user(request.user).filter(
        tenant_id=tenant_id,
        client_id=client.pk,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    )
    # email -> the open invitation, so a pending one can be cancelled from here.
    pending_by_email = {invite.email.lower(): invite.pk for invite in pending}
    state = assignment_state(client)
    return {
        "client": client,
        "page_title": client.legal_name,
        "engagements": [
            {
                "row": engagement,
                "service": SERVICE_LABELS.get(engagement.service_kind, ""),
                "responsible": team_names.get(str(engagement.responsible_id), ""),
            }
            for engagement in engagements
        ],
        "profiles": [
            {
                "row": profile,
                "regime": REGIME_LABELS.get(profile.regime, ""),
                "category": CATEGORY_LABELS.get(profile.mei_category or "", ""),
                "origin": ORIGIN_LABELS.get(profile.confidence, ""),
            }
            for profile in profiles
        ],
        "portal_roles": [
            {"value": value, "label": label} for value, label in portal_role_choices()
        ],
        "contacts": [
            {
                "row": contact,
                "pending_invite_id": pending_by_email.get(contact.email.lower()),
                "invitable": bool(
                    contact.active
                    and contact.email
                    and contact.channel_email_allowed
                    and contact.membership_id is None,
                ),
            }
            for contact in contacts
        ],
        "assignments": [
            {
                "row": assignment,
                "name": team_names.get(str(assignment.user_id), ""),
                "role": ASSIGNMENT_LABELS.get(assignment.role, ""),
            }
            for assignment in assignments
        ],
        "may_assign": can(request.user, ASSIGN_CAPABILITY, tenant_id=tenant_id),
        "engagement_form": forms.get("engagement_form")
        or EngagementForm(prefix="eng", team=team),
        "tax_form": forms.get("tax_form")
        or TaxProfileForm(
            prefix="tax",
            initial={"expected_revision": client.tax_profile_revision},
        ),
        "contact_form": forms.get("contact_form") or ContactForm(prefix="contato"),
        "invite_form": forms.get("invite_form") or ContactInviteForm(prefix="convite"),
        "assignment_state": state,
        "assignment_form": forms.get("assignment_form")
        or AssignmentForm(prefix="resp", team=team, initial={"state": state}),
    }


def _setup_page(
    request: AuthenticatedRequest,
    client: ClientCompany,
    status: int = HTTPStatus.OK,
    invite_outcome: str = "",
    **forms: Form,
) -> HttpResponse:
    context = _setup_context(request, client, **forms)
    context["invite_failed"] = {
        INVITE_FAILED: _(INVITE_SEND_FAILURE),
        INVITE_UNRESOLVED: _(INVITE_UNRESOLVED_NOTICE),
    }.get(invite_outcome, "")
    context["invite_sent"] = {
        INVITE_SENT: _(INVITE_SENT_NOTICE),
        INVITE_QUEUED: _(INVITE_QUEUED_NOTICE),
    }.get(invite_outcome, "")
    return render(request, SETUP_TEMPLATE, context, status=status)


def _to_setup(client_id: UUID, anchor: str = "") -> HttpResponseRedirect:
    url = reverse("client-setup", args=[client_id])
    return HttpResponseRedirect(f"{url}#{anchor}" if anchor else url)


def _conflict(request: AuthenticatedRequest, back_url: str) -> HttpResponse:
    return render(
        request,
        "clients/conflict.html",
        {"message": _(CONFLICT_MESSAGE), "back_url": back_url},
        status=HTTPStatus.CONFLICT,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def client_create_view(request: AuthenticatedRequest) -> HttpResponse:
    """Register a client from the CNPJ up, then continue to its setup page."""
    form = ClientCreateForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                client = create_client(
                    actor=request.user,
                    tenant_id=_tenant_id(request),
                    data=ClientData(
                        legal_name=data["legal_name"],
                        cnpj=data["cnpj"],
                        trade_name=data["trade_name"],
                        cpf=data["cpf"] or None,
                        state=data["state"],
                        municipality_ibge_code=data["municipality_ibge_code"],
                        main_cnae=data["main_cnae"],
                        opened_on=data["opened_on"],
                    ),
                )
            except ValidationError as error:
                _apply_errors(form, error)
            else:
                return _to_setup(client.pk)
        status = HTTPStatus.UNPROCESSABLE_ENTITY
    else:
        status = HTTPStatus.OK
    return render(
        request,
        "clients/client_form.html",
        {"form": form, "page_title": _("Novo cliente"), "client": None},
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def client_edit_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Edit a client's identity; the CNPJ is shown, never edited."""
    client = load_client(request.user, _tenant_id(request), pk)
    status = HTTPStatus.OK
    if request.method == "POST":
        form = ClientEditForm(request.POST)
        if form.is_valid():
            data = dict(form.cleaned_data)
            version = int(data.pop("version"))
            data["cpf"] = data["cpf"] or None
            try:
                update_client(
                    actor=request.user,
                    tenant_id=_tenant_id(request),
                    client_id=client.pk,
                    expected_version=version,
                    changes=data,
                )
            except ConflictError:
                return _conflict(request, reverse("client-edit", args=[client.pk]))
            except ValidationError as error:
                _apply_errors(form, error)
            else:
                return _to_setup(client.pk)
        status = HTTPStatus.UNPROCESSABLE_ENTITY
    else:
        form = ClientEditForm(
            initial={
                "legal_name": client.legal_name,
                "trade_name": client.trade_name,
                "cpf": client.cpf or "",
                "state": client.state,
                "municipality_ibge_code": client.municipality_ibge_code,
                "main_cnae": client.main_cnae,
                "opened_on": client.opened_on,
                "status": client.status,
                "version": client.version,
            },
        )
    return render(
        request,
        "clients/client_form.html",
        {"form": form, "page_title": client.legal_name, "client": client},
        status=status,
    )


@require_http_methods(["GET"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def client_setup_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Show the client's services, tax profile, contacts and responsible people.

    `?convite=` carries the outcome of the post-commit invitation send back here; it
    selects one of two fixed sentences and is never echoed.
    """
    client = load_client(request.user, _tenant_id(request), pk)
    return _setup_page(request, client, invite_outcome=request.GET.get("convite", ""))


def _engagement_data(form: EngagementForm) -> EngagementData:
    data = form.cleaned_data
    responsible = data.get("responsible") or None
    return EngagementData(
        service_kind=data["service_kind"],
        starts_on=data["starts_on"],
        ends_on=data.get("ends_on"),
        responsible_id=UUID(responsible) if responsible else None,
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def engagement_create_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Add a recurring service to the client."""
    tenant_id = _tenant_id(request)
    client = load_client(request.user, tenant_id, pk)
    form = EngagementForm(request.POST, prefix="eng", team=_team(tenant_id))
    if form.is_valid():
        try:
            save_engagement(
                actor=request.user,
                tenant_id=tenant_id,
                client_id=client.pk,
                data=_engagement_data(form),
            )
        except ValidationError as error:
            _apply_errors(form, error)
        else:
            return _to_setup(client.pk, "servicos")
    return _setup_page(
        request,
        client,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        engagement_form=form,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def engagement_edit_view(
    request: AuthenticatedRequest,
    pk: UUID,
    engagement_pk: UUID,
) -> HttpResponse:
    """Edit one engagement, carrying the version it read."""
    tenant_id = _tenant_id(request)
    client = load_client(request.user, tenant_id, pk)
    engagement = Engagement.objects.filter(client=client, pk=engagement_pk).first()
    if engagement is None:
        raise Http404
    team = _team(tenant_id)
    status = HTTPStatus.OK
    if request.method == "POST":
        form = EngagementForm(request.POST, prefix="eng", team=team)
        if form.is_valid():
            try:
                save_engagement(
                    actor=request.user,
                    tenant_id=tenant_id,
                    client_id=client.pk,
                    data=_engagement_data(form),
                    engagement_id=engagement.pk,
                    expected_version=form.cleaned_data.get("version"),
                )
            except ConflictError:
                return _conflict(request, request.path)
            except ValidationError as error:
                _apply_errors(form, error)
            else:
                return _to_setup(client.pk, "servicos")
        status = HTTPStatus.UNPROCESSABLE_ENTITY
    else:
        form = EngagementForm(
            prefix="eng",
            team=team,
            initial={
                "service_kind": engagement.service_kind,
                "starts_on": engagement.starts_on,
                "ends_on": engagement.ends_on,
                "responsible": str(engagement.responsible_id or ""),
                "version": engagement.version,
            },
        )
    return render(
        request,
        "clients/row_form.html",
        {
            "client": client,
            "form": form,
            "page_title": _("Editar serviço"),
            "heading": _("Editar serviço"),
        },
        status=status,
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def tax_profile_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Record a regime claim through `correct_tax_profile` with the revision read."""
    tenant_id = _tenant_id(request)
    client = load_client(request.user, tenant_id, pk)
    form = TaxProfileForm(request.POST, prefix="tax")
    if form.is_valid():
        data = form.cleaned_data
        try:
            set_tax_profile(
                actor=request.user,
                tenant_id=tenant_id,
                client_id=client.pk,
                regime=data["regime"],
                mei_category=data["mei_category"] or None,
                valid_from=data["valid_from"],
                valid_to=data.get("valid_to"),
                note=data.get("note", ""),
                expected_revision=data["expected_revision"],
            )
        except ConflictError:
            return _conflict(request, reverse("client-setup", args=[client.pk]))
        except PermissionDenied:
            raise
        except ValidationError as error:
            _apply_errors(form, error)
        else:
            return _to_setup(client.pk, "perfil")
    return _setup_page(
        request,
        client,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        tax_form=form,
    )


def _contact_data(form: ContactForm) -> ContactData:
    data = form.cleaned_data
    return ContactData(
        name=data["name"],
        email=data.get("email", ""),
        phone=data.get("phone", ""),
        role_label=data.get("role_label", ""),
        channel_email_allowed=bool(data.get("channel_email_allowed")),
        is_primary=bool(data.get("is_primary")),
        active=bool(data.get("is_active")),
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def contact_create_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Add a person at the client."""
    tenant_id = _tenant_id(request)
    client = load_client(request.user, tenant_id, pk)
    form = ContactForm(request.POST, prefix="contato")
    if form.is_valid():
        try:
            save_contact(
                actor=request.user,
                tenant_id=tenant_id,
                client_id=client.pk,
                data=_contact_data(form),
            )
        except ValidationError as error:
            _apply_errors(form, error)
        else:
            return _to_setup(client.pk, "contatos")
    return _setup_page(
        request,
        client,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        contact_form=form,
    )


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(MANAGE_CAPABILITY)
def contact_edit_view(
    request: AuthenticatedRequest,
    pk: UUID,
    contact_pk: UUID,
) -> HttpResponse:
    """Edit or deactivate a contact, carrying the version it read."""
    tenant_id = _tenant_id(request)
    client, contact = load_contact(request.user, tenant_id, pk, contact_pk)
    status = HTTPStatus.OK
    if request.method == "POST":
        form = ContactForm(request.POST, prefix="contato")
        if form.is_valid():
            try:
                save_contact(
                    actor=request.user,
                    tenant_id=tenant_id,
                    client_id=client.pk,
                    data=_contact_data(form),
                    contact_id=contact.pk,
                    expected_version=form.cleaned_data.get("version"),
                )
            except ConflictError:
                return _conflict(request, request.path)
            except ValidationError as error:
                _apply_errors(form, error)
            else:
                return _to_setup(client.pk, "contatos")
        status = HTTPStatus.UNPROCESSABLE_ENTITY
    else:
        form = ContactForm(
            prefix="contato",
            initial={
                "name": contact.name,
                "email": contact.email,
                "phone": contact.phone,
                "role_label": contact.role_label,
                "channel_email_allowed": contact.channel_email_allowed,
                "is_primary": contact.is_primary,
                "is_active": contact.active,
                "version": contact.version,
            },
        )
    return render(
        request,
        "clients/row_form.html",
        {
            "client": client,
            "form": form,
            "page_title": _("Editar contato"),
            "heading": _("Editar contato"),
        },
        status=status,
    )


def _mail_contact_invitation(
    email: str,
    tenant: Tenant,
    raw_token: str,
) -> None:
    """Send through the same single outbound path the portal invite uses."""
    # Imported here: the helper is private to the accounts views module, and this
    # is the one other caller that must use exactly that path and wording.
    from apps.accounts.views import _mail_portal_invitation  # noqa: PLC0415

    _mail_portal_invitation(email, tenant.name, tenant.slug, raw_token)


def _deliver_invitation(  # noqa: PLR0913 - everything the post-commit send needs
    *,
    response: HttpResponseRedirect,
    setup_url: str,
    actor: User,
    invite: Invite,
    tenant: Tenant,
    raw_token: str,
) -> None:
    """Mail a committed invitation; on refusal, stand it down and say so.

    Runs from `transaction.on_commit`, after the tenant transaction has committed:
    the send is external I/O and must never happen inside it (plan MH-6/MN-3). The
    outcome is written into the redirect the view already returned, so the reader
    lands on the setup page with a message either way. An SMTP refusal withdraws
    the invitation through `withdraw_undelivered_invite` (system cleanup of that one
    row, audited) so no live credential exists that nobody received, and the contact
    can be invited again. If even that cleanup fails, the page says the invitation
    may still be live and offers "Cancelar convite pendente" as the recovery.
    """
    try:
        _mail_contact_invitation(invite.email, tenant, raw_token)
    except (SMTPException, OSError) as error:
        logger.warning(
            "contact invitation mail refused",
            extra={"invite_id": str(invite.pk), "tenant_id": str(tenant.pk)},
        )
        sentry_sdk.capture_exception(error)
        try:
            withdraw_undelivered_invite(
                invite_id=invite.pk,
                requested_by=actor,
                reason=type(error).__name__,
            )
        except Exception as cleanup_error:
            logger.exception(
                "could not stand down an unsent invitation",
                extra={"invite_id": str(invite.pk)},
            )
            sentry_sdk.capture_exception(cleanup_error)
            response["Location"] = f"{setup_url}?convite={INVITE_UNRESOLVED}#contatos"
            return
        response["Location"] = f"{setup_url}?convite={INVITE_FAILED}#contatos"
        return
    response["Location"] = f"{setup_url}?convite={INVITE_SENT}#contatos"


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ASSIGN_CAPABILITY)
def contact_invite_view(
    request: AuthenticatedRequest,
    pk: UUID,
    contact_pk: UUID,
) -> HttpResponse:
    """Invite a contact into this client's portal, at the contact's own address.

    `invite_contact` issues under the client and contact locks, against the
    contact version the page was rendered with and the contact's current e-mail
    permission, and refuses a second live invitation (409). The mail is sent only
    after the request transaction commits (`_deliver_invitation`), and the result
    comes back to the setup page as a message: sent, or not sent and retryable.
    """
    tenant = _tenant(request)
    tenant_id = _tenant_id(request)
    client, contact = load_contact(request.user, tenant_id, pk, contact_pk)
    form = ContactInviteForm(request.POST, prefix="convite")
    setup_url = reverse("client-setup", args=[client.pk])
    if not form.is_valid():
        return _setup_page(
            request,
            client,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            invite_form=form,
        )
    try:
        invite, raw_token = invite_contact(
            actor=request.user,
            tenant=tenant,
            client_id=client.pk,
            contact_id=contact.pk,
            role=form.cleaned_data["role"],
            expected_version=form.cleaned_data["version"],
        )
    except ConflictError:
        return _conflict(request, setup_url)
    except InviteNotPermittedError as error:
        raise PermissionDenied(str(error)) from error
    except ValidationError as error:
        _apply_errors(form, error)
        return _setup_page(
            request,
            client,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            invite_form=form,
        )
    if tenant_has(tenant.pk, "cycles_r1"):
        # Same transaction as the invitation, under the locks invite_contact took:
        # the message commits with the invite or not at all, and no I/O runs here.
        from apps.delivery.outbox import enqueue_portal_invite  # noqa: PLC0415

        enqueue_portal_invite(request.user, invite, contact)
        return HttpResponseRedirect(f"{setup_url}?convite={INVITE_QUEUED}#contatos")
    response = HttpResponseRedirect(f"{setup_url}#contatos")
    transaction.on_commit(
        partial(
            _deliver_invitation,
            response=response,
            setup_url=setup_url,
            actor=request.user,
            invite=invite,
            tenant=tenant,
            raw_token=raw_token,
        ),
    )
    return response


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ASSIGN_CAPABILITY)
def assignment_add_view(request: AuthenticatedRequest, pk: UUID) -> HttpResponse:
    """Put a member of the firm on this client."""
    tenant_id = _tenant_id(request)
    client = load_client(request.user, tenant_id, pk)
    form = AssignmentForm(request.POST, prefix="resp", team=_team(tenant_id))
    if form.is_valid():
        try:
            add_assignment(
                actor=request.user,
                tenant_id=tenant_id,
                client_id=client.pk,
                user_id=UUID(form.cleaned_data["user"]),
                role=form.cleaned_data["role"],
                expected_state=form.cleaned_data["state"],
            )
        except ConflictError:
            return _conflict(request, reverse("client-setup", args=[client.pk]))
        except ValidationError as error:
            _apply_errors(form, error)
        else:
            return _to_setup(client.pk, "responsaveis")
    return _setup_page(
        request,
        client,
        HTTPStatus.UNPROCESSABLE_ENTITY,
        assignment_form=form,
    )


@require_POST
@login_required
@require_tenant
@require_feature(FLAG)
@require_can(ASSIGN_CAPABILITY)
def assignment_remove_view(
    request: AuthenticatedRequest,
    pk: UUID,
    assignment_pk: UUID,
) -> HttpResponse:
    """Take a member of the firm off this client."""
    tenant_id = _tenant_id(request)
    client = load_client(request.user, tenant_id, pk)
    try:
        remove_assignment(
            actor=request.user,
            tenant_id=tenant_id,
            client_id=client.pk,
            assignment_id=assignment_pk,
            expected_state=request.POST.get("state", ""),
        )
    except ConflictError:
        return _conflict(request, reverse("client-setup", args=[client.pk]))
    return _to_setup(client.pk, "responsaveis")


__all__ = [
    "assignment_add_view",
    "assignment_remove_view",
    "client_create_view",
    "client_edit_view",
    "client_setup_view",
    "contact_create_view",
    "contact_edit_view",
    "contact_invite_view",
    "engagement_create_view",
    "engagement_edit_view",
    "tax_profile_view",
]
