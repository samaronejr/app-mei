"""Admin registration for the client registry, with document-aware search.

The one non-obvious behaviour here is `get_search_results`. Django's `search_fields`
compares the raw term against the raw column, so an operator pasting a masked
`12.ABC.345/01DE-35` out of an email would get no results from a column that stores
`12ABC34501DE35` — and would reasonably conclude the client is not on the books.
The term is normalized the same way the column was before it is compared.
"""

from typing import Any, ClassVar

from django import forms
from django.contrib import admin
from django.db.models import Q, QuerySet
from django.http import HttpRequest

from apps.audit.admin import TenantScopedAdminMixin
from apps.clients.models import ClientCompany
from apps.core.tenancy import current_tenant_id
from apps.fiscal.formatting import format_cnpj
from apps.fiscal.validators import CNPJ_ALPHABET, normalize_document

# A masked CNPJ is eighteen characters and a masked CPF is fourteen, while the columns
# hold fourteen and eleven. The form fields are widened to the masked length and
# normalized on the way in; without this the form's own max_length would reject a
# pasted, punctuated document before the model ever saw it.
_MASKED_CNPJ_LENGTH = 18
_MASKED_CPF_LENGTH = 14


class ClientCompanyForm(forms.ModelForm[ClientCompany]):
    """Accept a document however it was punctuated, and store it normalized."""

    cnpj = forms.CharField(label="CNPJ", max_length=_MASKED_CNPJ_LENGTH)
    cpf = forms.CharField(label="CPF", max_length=_MASKED_CPF_LENGTH, required=False)

    class Meta:
        """Form metadata.

        The fields are enumerated rather than taken as `"__all__"`. A wildcard would
        put `tenant` on the form as an editable dropdown, which is a cross-tenant move
        primitive: an operator could reassign a client to another firm, and row-level
        security would allow the write because the row's new tenant is the one in
        context. Enumerating is also why a column added later cannot appear on this
        form by accident.
        """

        model = ClientCompany
        fields: ClassVar[list[str]] = [
            "legal_name",
            "trade_name",
            "cnpj",
            "cpf",
            "municipality_ibge_code",
            "state",
            "main_cnae",
            "opened_on",
            "status",
            "is_mei",
            "govbr_trust_level",
            "has_digital_certificate",
            "certificate_expires_on",
            "has_employee",
        ]

    def clean_cnpj(self) -> str:
        """Normalize before the model's check-digit validator sees the value."""
        return normalize_document(self.cleaned_data["cnpj"])

    def clean_cpf(self) -> str:
        """Normalize before the model's check-digit validator sees the value."""
        return normalize_document(self.cleaned_data["cpf"])


@admin.register(ClientCompany)
class ClientCompanyAdmin(TenantScopedAdminMixin, admin.ModelAdmin[ClientCompany]):
    """Browse one firm's MEI clients, searchable by masked or bare document."""

    form = ClientCompanyForm
    list_display = ("legal_name", "masked_cnpj_column", "status", "state")
    list_filter = ("status", "is_mei", "state")
    search_fields = ("legal_name", "trade_name", "cnpj", "cpf")
    readonly_fields: ClassVar[tuple[str, ...]] = ("created_at", "updated_at")

    @admin.display(description="CNPJ", ordering="cnpj")
    def masked_cnpj_column(self, obj: ClientCompany) -> str:
        """Show the CNPJ in the mask an accountant reads, not the stored form."""
        return format_cnpj(obj.cnpj)

    def get_search_results(
        self,
        request: HttpRequest,
        queryset: QuerySet[ClientCompany],
        search_term: str,
    ) -> tuple[QuerySet[ClientCompany], bool]:
        """Match a pasted, masked document against the normalized column."""
        results, may_have_duplicates = super().get_search_results(
            request,
            queryset,
            search_term,
        )

        normalized = normalize_document(search_term.strip())
        if not normalized or any(char not in CNPJ_ALPHABET for char in normalized):
            return results, may_have_duplicates

        # Unioned with the default result rather than replacing it: a term such as
        # "MEI" is both a plausible name fragment and a valid document fragment, and
        # narrowing to the document match would lose the name hits.
        widened = queryset.filter(
            Q(cnpj__contains=normalized) | Q(cpf__contains=normalized),
        )
        return results | widened, True

    def save_model(
        self,
        request: HttpRequest,
        obj: ClientCompany,
        form: forms.ModelForm[ClientCompany],
        change: bool,
    ) -> None:
        """Stamp the ambient firm onto a new row, and never onto an existing one.

        `ClientCompanyForm` omits `tenant` on purpose — an editable dropdown would be
        a cross-tenant move primitive — so nothing else puts a firm on the INSERT.
        Left NULL, the row-level-security predicate `tenant_id = app.tenant_id` is
        `NULL = <uuid>`, which is never true, and the add dies on the policy.

        On change the tenant is not touched: the form cannot move a row across firms
        anyway, and re-stamping would only hide a context that had gone missing.
        A missing context on add is likewise left alone rather than guessed at — the
        write then fails closed on the policy, which is the designed refusal.
        """
        if not change:
            tenant_id = current_tenant_id.get()
            if tenant_id is not None:
                obj.tenant_id = tenant_id
        super().save_model(request, obj, form, change)

    def has_delete_permission(
        self,
        request: HttpRequest,  # noqa: ARG002
        obj: Any = None,  # noqa: ANN401, ARG002
    ) -> bool:
        """Refuse deletion from the admin.

        A client carries onboarding items, assignments and an audit trail, and the
        retention rules in `docs/retention.md` govern when any of it may go. Closing a
        client is a status change; erasure is a rights request, handled through the
        LGPD intake so that it is recorded.
        """
        return False
