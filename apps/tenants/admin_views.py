"""The tenant selector itself."""

from http import HTTPStatus
from uuid import UUID

from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.tenants.admin_tenancy import (
    ADMIN_TENANT_SESSION_KEY,
    AdminTenantError,
    authorize,
    record_selection,
    selectable_tenants,
)

TEMPLATE = "admin/select_tenant.html"
DEFAULT_DESTINATION = "/admin/"
TENANT_LABEL = gettext_lazy("Empresa")


class OpenChoiceField(forms.ChoiceField):
    """A dropdown that enforces presence and leaves membership to `authorize`.

    `ChoiceField` normally refuses any value outside its own option list, and here that
    would be the wrong control in both directions.

    It adds nothing: `authorize` re-checks the submitted id against the operator's
    memberships on every POST, and that check is the only thing standing between one
    `is_staff` account and every firm's CNPJs, CPFs and audit stream. The option list
    is a UI convenience — the value arrives as a request parameter, which is why
    `admin_tenancy`'s docstring says filtering the dropdown changes nothing.

    And it would break the escape hatch: `selectable_tenants` returns the operator's
    OWN firms, so the firm a superuser opens under break-glass is by definition absent
    from their options. Validating against the list would answer every break-glass
    attempt with a 403 and leave the reason requirement unreachable.
    """

    def valid_value(self, value: object) -> bool:
        """Accept anything not blank; the view resolves the id and authorizes it."""
        return bool(str(value).strip())


class SelectTenantForm(forms.Form):
    """The selector's POST contract — `tenant`, `reason`, `next` — unchanged.

    The field names are the wire format this view has always read, so they are spelled
    exactly as they were when the template hand-rolled the controls. What the form adds
    is a bound object the shared partials can render: an `id_tenant` to mark invalid
    and an error the summary can link to.
    """

    tenant = OpenChoiceField(label=TENANT_LABEL)
    reason = forms.CharField(
        label=gettext_lazy("Motivo (obrigatório para acesso fora das suas empresas)"),
        required=False,
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(
        self,
        data: QueryDict | None = None,
        *,
        user: User,
        initial: dict[str, str] | None = None,
    ) -> None:
        """Offer this operator's own firms, and the reason box only if it applies."""
        super().__init__(data, initial=initial)
        self.fields["tenant"] = OpenChoiceField(
            label=TENANT_LABEL,
            choices=[(str(firm.pk), firm.name) for firm in selectable_tenants(user)],
        )
        if not user.is_superuser:
            del self.fields["reason"]


@staff_member_required
@require_http_methods(["GET", "POST"])
def select_tenant_view(request: HttpRequest) -> HttpResponse:
    """Choose the firm this admin session operates as, and record the choice."""
    user = request.user
    assert isinstance(user, User)  # noqa: S101 — staff_member_required guarantees it

    if request.method == "GET":
        destination = request.GET.get("next") or DEFAULT_DESTINATION
        return render(
            request,
            TEMPLATE,
            _context(SelectTenantForm(user=user, initial={"next": destination})),
        )

    form = SelectTenantForm(request.POST, user=user)
    if not form.is_valid():
        return render(request, TEMPLATE, _context(form), status=HTTPStatus.FORBIDDEN)

    reason = str(form.cleaned_data.get("reason", ""))
    try:
        tenant, break_glass = authorize(
            user=user,
            tenant_id=UUID(str(form.cleaned_data["tenant"])),
            reason=reason,
        )
    except (AdminTenantError, ValueError) as error:
        # Kept as a notice above the form rather than folded into the field errors: a
        # refusal here is a decision about the ACCOUNT, not about what was typed, and
        # attaching "no membership for that firm" to the dropdown would invite the
        # reader to pick a different option as though that were the problem.
        return render(
            request,
            TEMPLATE,
            _context(form, error=str(error) or _("Empresa inválida.")),
            status=HTTPStatus.FORBIDDEN,
        )

    record_selection(user=user, tenant=tenant, break_glass=break_glass, reason=reason)
    request.session[ADMIN_TENANT_SESSION_KEY] = str(tenant.pk)
    destination = str(form.cleaned_data.get("next") or DEFAULT_DESTINATION)
    return HttpResponseRedirect(destination)


def _context(form: SelectTenantForm, error: str = "") -> dict[str, object]:
    return {"form": form, "error": error}
