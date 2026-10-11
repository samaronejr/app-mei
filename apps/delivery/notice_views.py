"""Private ordinary-owner notice settings, available through either R1 shell."""

from http import HTTPStatus

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.views.decorators.http import require_http_methods

from apps.accounts.views import AuthenticatedRequest
from apps.clients.views import require_tenant
from apps.delivery.errors import VersionConflict
from apps.delivery.models import AutomationPause, TenantNoticePolicy
from apps.delivery.notice_authority import (
    NOTICE_CHOICES,
    NoticeRefusalError,
    configure,
    enabled,
    granted_kinds,
    may_manage,
    tenant_id,
)
from apps.delivery.services import set_pause
from apps.tenants.models import Membership, Tenant


class NoticePolicyForm(forms.Form):
    """Only actual implemented manual grants; no basis or automatic-rule input."""

    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    kinds = forms.MultipleChoiceField(
        required=False,
        choices=NOTICE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        label="Tipos manuais autorizados",
    )
    acknowledged = forms.BooleanField(
        required=False,
        label=(
            "Autorizo somente os tipos manuais marcados para os destinatários "
            "elegíveis deste escritório. Esta autorização não vale para entregas "
            "que exigem aprovação."
        ),
    )


class PauseForm(forms.Form):
    """The existing tenant dispatch pause has its own concurrency revision."""

    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=(("pause", "pause"), ("resume", "resume")))


def _policy_state(policy: TenantNoticePolicy | None, tenant: Tenant) -> str:
    if policy is None:
        return "absent"
    if not policy.grants_json:
        return "revoked"
    owner = policy.acknowledged_by
    if owner is None:
        return "unacknowledged"
    if policy.ack_membership_id is None or policy.ack_membership_version is None:
        return "stale"
    # PLATFORM_QUERY_OK: only the exact recorded owner seat determines whether
    # this private policy's acknowledgement is still current.
    member = Membership.objects.filter(
        pk=policy.ack_membership_id,
        tenant=tenant,
        client=None,
        user_id=owner.pk,
        version=policy.ack_membership_version,
    ).first()
    return (
        "authorized"
        if member is not None
        and may_manage(owner, tenant)
        and policy.acknowledged_version == policy.version
        else "stale"
    )


def _save_policy(
    request: AuthenticatedRequest, form: NoticePolicyForm, action: str
) -> HTTPStatus:
    if action not in {"save", "revoke"}:
        form.add_error(None, "Ação indisponível.")
        return HTTPStatus.UNPROCESSABLE_ENTITY
    if not form.is_valid():
        return HTTPStatus.UNPROCESSABLE_ENTITY
    try:
        configure(
            request.user,
            kinds=tuple(form.cleaned_data["kinds"]) if action == "save" else (),
            acknowledged=form.cleaned_data["acknowledged"]
            if action == "save"
            else False,
            expected_version=form.cleaned_data["expected_version"],
        )
    except NoticeRefusalError as error:
        form.add_error(None, error)
        return (
            HTTPStatus.CONFLICT
            if error.reason == "notice_policy_changed"
            else HTTPStatus.UNPROCESSABLE_ENTITY
        )
    except ValidationError as error:
        form.add_error(None, error)
        return HTTPStatus.UNPROCESSABLE_ENTITY
    else:
        return HTTPStatus.FOUND


def _pause(request: AuthenticatedRequest) -> tuple[HTTPStatus, str]:
    form = PauseForm(request.POST)
    if not form.is_valid():
        return HTTPStatus.UNPROCESSABLE_ENTITY, "Ação ou revisão da pausa inválida."
    try:
        set_pause(
            request.user,
            paused=form.cleaned_data["action"] == "pause",
            expected_version=form.cleaned_data["expected_version"],
        )
    except VersionConflict as error:
        return HTTPStatus.CONFLICT, str(error)
    else:
        return HTTPStatus.FOUND, ""


@require_http_methods(["GET", "POST"])
@login_required
@require_tenant
def notice_policy(request: AuthenticatedRequest) -> HttpResponse:
    """GET is a read, never initial consent; every POST repeats fresh admission."""
    # PLATFORM_QUERY_OK: tenant_context has admitted this exact firm.
    tenant = Tenant.objects.get(pk=tenant_id())
    if not enabled(tenant):
        raise Http404
    if not may_manage(request.user, tenant):
        raise PermissionDenied
    policy = TenantNoticePolicy.objects.filter().first()
    policy_state = _policy_state(policy, tenant)
    control = AutomationPause.objects.filter().first()
    form = NoticePolicyForm(
        initial={
            "expected_version": policy.version if policy else 0,
            "kinds": granted_kinds(policy),
        }
    )
    status = HTTPStatus.OK
    pause_error = ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action in {"pause", "resume"}:
            status, pause_error = _pause(request)
        else:
            form = NoticePolicyForm(request.POST)
            status = _save_policy(request, form, action)
        if status == HTTPStatus.FOUND:
            return redirect("notice-policy")
    return TemplateResponse(
        request,
        "delivery/notice_policy.html",
        {
            "form": form,
            "policy": policy,
            "policy_state": policy_state,
            "control": control,
            "pause_version": control.version if control else 1,
            "pause_error": pause_error,
            "global_paused": settings.DELIVERY_PAUSED,
        },
        status=status,
    )
