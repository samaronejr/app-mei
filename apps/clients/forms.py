"""Forms for the in-product onboarding screens (AXR1-007).

Server validation is the authority (plan S5.6). Each form is rendered through
`partials/pure/field.html`, which owns the label binding and the
`aria-describedby`/`aria-invalid` contract, and every editor of an existing row
carries the `version` it read as a hidden field.
"""

from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.accounts.forms import portal_role_choices
from apps.clients.importing import FIELDS, document_messages
from apps.clients.models import (
    AssignmentRole,
    ClientStatus,
    ServiceKind,
    TaxRegime,
)
from apps.fiscal.mei import MEICategory
from apps.fiscal.models import UF
from apps.fiscal.validators import normalize_document, validate_cnpj, validate_cpf

STATUS_CHOICES = [
    (ClientStatus.ONBOARDING, _("Em implantação")),
    (ClientStatus.ACTIVE, _("Ativo")),
    (ClientStatus.SUSPENDED, _("Suspenso")),
    (ClientStatus.CLOSED, _("Encerrado")),
]
SERVICE_CHOICES = [
    (ServiceKind.MEI_MENSAL, _("MEI mensal")),
    (ServiceKind.DOCUMENTOS_MENSAL, _("Coleta mensal de documentos")),
    (ServiceKind.OUTRO, _("Outro serviço")),
]
REGIME_CHOICES = [
    (TaxRegime.MEI, _("MEI")),
    (TaxRegime.SIMPLES, _("Simples Nacional")),
    (TaxRegime.PRESUMIDO, _("Lucro Presumido")),
    (TaxRegime.REAL, _("Lucro Real")),
    (TaxRegime.ISENTO, _("Isento")),
    (TaxRegime.UNKNOWN, _("Sem informação")),
]
MEI_CATEGORY_CHOICES = [
    ("", _("Não se aplica")),
    (MEICategory.COMMON, _("MEI comum")),
    (MEICategory.CAMINHONEIRO, _("MEI caminhoneiro")),
]
ASSIGNMENT_ROLE_CHOICES = [
    (AssignmentRole.PRIMARY, _("Responsável principal")),
    (AssignmentRole.SUPPORT, _("Apoio")),
]
UF_CHOICES = [("", _("Não informada")), *[(uf.value, uf.value) for uf in UF]]


class DateInput(forms.DateInput):
    """A native date picker; the partial reads `input_type`."""

    input_type = "date"

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        """Render ISO dates, the only format a date input accepts."""
        super().__init__(attrs=attrs, format="%Y-%m-%d")


def _document_digits(value: str) -> str:
    return normalize_document(value or "")


class ClientIdentityForm(forms.Form):
    """Identity fields shared by the create and edit screens."""

    legal_name = forms.CharField(label=_("Razão social"), max_length=255)
    trade_name = forms.CharField(
        label=_("Nome fantasia"),
        max_length=255,
        required=False,
    )
    cpf = forms.CharField(label=_("CPF do titular"), max_length=14, required=False)
    state = forms.ChoiceField(label=_("UF"), choices=UF_CHOICES, required=False)
    municipality_ibge_code = forms.RegexField(
        label=_("Código IBGE do município"),
        regex=r"^\d{7}$",
        required=False,
        max_length=7,
        error_messages={"invalid": _("Informe os 7 dígitos do código IBGE.")},
    )
    main_cnae = forms.CharField(label=_("CNAE principal"), max_length=9, required=False)
    opened_on = forms.DateField(
        label=_("Data de abertura"),
        required=False,
        widget=DateInput(),
    )

    def clean_cpf(self) -> str:
        """Normalize and validate the owner's CPF when given."""
        cpf = _document_digits(self.cleaned_data.get("cpf", ""))
        if cpf:
            try:
                validate_cpf(cpf)
            except ValidationError as error:
                raise ValidationError(document_messages(error, "CPF")) from error
        return cpf

    def clean_main_cnae(self) -> str:
        """Accept the masked form (47.51-2/01) and store the seven digits."""
        cnae = "".join(
            ch for ch in self.cleaned_data.get("main_cnae", "") if ch.isalnum()
        )
        if cnae and not (len(cnae) == 7 and cnae.isdigit()):  # noqa: PLR2004 - CNAE width
            raise ValidationError(_("Informe os 7 dígitos do CNAE."))
        return cnae


class ClientCreateForm(ClientIdentityForm):
    """The create screen adds the CNPJ, which the editor never changes."""

    cnpj = forms.CharField(label=_("CNPJ"), max_length=18)

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Put the CNPJ first: it is what the person has in hand."""
        super().__init__(*args, **kwargs)
        self.order_fields(["cnpj", "legal_name"])

    def clean_cnpj(self) -> str:
        """Normalize (alphanumeric, uppercase, no mask) and check the digits."""
        cnpj = _document_digits(self.cleaned_data.get("cnpj", ""))
        try:
            validate_cnpj(cnpj)
        except ValidationError as error:
            raise ValidationError(document_messages(error, "CNPJ")) from error
        return cnpj


class ClientEditForm(ClientIdentityForm):
    """The identity editor, carrying the version it read."""

    status = forms.ChoiceField(label=_("Situação"), choices=STATUS_CHOICES)
    version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)


class EngagementForm(forms.Form):
    """One recurring service for the client."""

    service_kind = forms.ChoiceField(label=_("Serviço"), choices=SERVICE_CHOICES)
    starts_on = forms.DateField(label=_("Início"), widget=DateInput())
    ends_on = forms.DateField(label=_("Fim"), required=False, widget=DateInput())
    responsible = forms.ChoiceField(label=_("Responsável"), required=False)
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(
        self,
        *args: Any,  # noqa: ANN401
        team: list[tuple[str, str]],
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Offer the firm's own roster as the responsible person."""
        super().__init__(*args, **kwargs)
        responsible = cast("forms.ChoiceField", self.fields["responsible"])
        responsible.choices = [("", _("Ninguém ainda")), *team]


class TaxProfileForm(forms.Form):
    """A regime claim from a date, recorded through the correction service."""

    regime = forms.ChoiceField(label=_("Regime"), choices=REGIME_CHOICES)
    mei_category = forms.ChoiceField(
        label=_("Categoria MEI"),
        choices=MEI_CATEGORY_CHOICES,
        required=False,
    )
    valid_from = forms.DateField(label=_("Vale a partir de"), widget=DateInput())
    valid_to = forms.DateField(label=_("Vale até"), required=False, widget=DateInput())
    note = forms.CharField(
        label=_("Observação"),
        required=False,
        widget=forms.Textarea,
        help_text=_("Obrigatória ao alterar um período passado já registrado."),
    )
    expected_revision = forms.IntegerField(widget=forms.HiddenInput, min_value=1)

    def clean(self) -> dict[str, Any]:
        """Require a category for MEI and refuse one for any other regime."""
        cleaned = super().clean() or {}
        regime = cleaned.get("regime")
        category = cleaned.get("mei_category") or ""
        if regime == TaxRegime.MEI and not category:
            self.add_error("mei_category", _("Escolha a categoria do MEI."))
        if regime != TaxRegime.MEI and category:
            self.add_error("mei_category", _("Categoria só se aplica ao MEI."))
        valid_from, valid_to = cleaned.get("valid_from"), cleaned.get("valid_to")
        if valid_from and valid_to and valid_to <= valid_from:
            self.add_error("valid_to", _("O fim precisa ser depois do início."))
        return cleaned


class ContactForm(forms.Form):
    """A person at the client the firm talks to."""

    name = forms.CharField(label=_("Nome"), max_length=255)
    email = forms.EmailField(label=_("E-mail"), required=False)
    phone = forms.CharField(label=_("Telefone"), max_length=32, required=False)
    role_label = forms.CharField(label=_("Função"), max_length=64, required=False)
    channel_email_allowed = forms.BooleanField(
        label=_("Pode receber e-mails"),
        required=False,
        initial=True,
    )
    is_primary = forms.BooleanField(label=_("Contato principal"), required=False)
    is_active = forms.BooleanField(label=_("Ativo"), required=False, initial=True)
    version = forms.IntegerField(widget=forms.HiddenInput, required=False)


class ContactInviteForm(forms.Form):
    """Which portal seat the contact is invited to; the address is the contact's."""

    role = forms.ChoiceField(label=_("Papel no portal"), choices=portal_role_choices)
    # The contact version the page was rendered with; a stale one is refused.
    version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)


class AssignmentForm(forms.Form):
    """A firm member put on this client."""

    user = forms.ChoiceField(label=_("Pessoa da equipe"))
    role = forms.ChoiceField(label=_("Papel"), choices=ASSIGNMENT_ROLE_CHOICES)
    state = forms.CharField(widget=forms.HiddenInput, max_length=64)

    def __init__(
        self,
        *args: Any,  # noqa: ANN401
        team: list[tuple[str, str]],
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Offer the firm's own roster."""
        super().__init__(*args, **kwargs)
        user = cast("forms.ChoiceField", self.fields["user"])
        user.choices = team


def _column_chooser(label: Any, options: list[tuple[str, Any]]) -> forms.ChoiceField:  # noqa: ANN401
    """One optional column choice, labelled with the pt-BR name of its field."""
    return forms.ChoiceField(label=label, choices=options, required=False)


class ImportUploadForm(forms.Form):
    """The file and, when the headers were not recognized, the column choices."""

    file = forms.FileField(
        label=_("Arquivo CSV ou XLSX"),
        help_text=_("Até 5 MiB e 5.000 linhas. A primeira linha é o cabeçalho."),
    )

    def __init__(
        self,
        *args: Any,  # noqa: ANN401
        headers: list[str] | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        """Add one column chooser per field once the file's headers are known."""
        super().__init__(*args, **kwargs)
        if headers is None:
            return
        options = [("", _("Não importar")), *[(h, h) for h in headers if h]]
        for field in FIELDS:
            self.fields[f"map_{field.key}"] = _column_chooser(field.label, options)


def posted_mapping(data: dict[str, Any]) -> dict[str, str] | None:
    """Read the column choices off a re-submitted upload, or None on a first upload.

    Read from the POST rather than from bound fields because the choices only exist
    once the file's headers are known; `resolve_mapping` re-validates every chosen
    header against the file that actually arrived.
    """
    keys = [f"map_{field.key}" for field in FIELDS]
    if not any(key in data for key in keys):
        return None
    return {field.key: str(data.get(f"map_{field.key}", "")) for field in FIELDS}


class VersionForm(forms.Form):
    """A bare version, for the commit and rollback buttons."""

    version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
