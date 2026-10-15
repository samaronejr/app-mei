"""Rendered revisions and bounded inputs for firm and platform settings."""

from django import forms
from django.core.files.uploadedfile import UploadedFile

from apps.clients.forms import DateInput
from apps.tenants.branding import validate_logo
from apps.tenants.models import BillingStatus


class FirmRegistrationForm(forms.Form):
    """The assisted Nova firma flow, with explicit manual entitlements."""

    name = forms.CharField(label="Nome da firma", max_length=255)
    slug = forms.SlugField(label="Subdomínio", max_length=63)
    owner_email = forms.EmailField(label="E-mail do responsável")
    plan_band = forms.CharField(label="Faixa contratada", max_length=64, required=False)
    active_client_limit = forms.IntegerField(
        label="Limite de clientes ativos",
        min_value=0,
        required=False,
        help_text="Em branco: sem limite. Zero: nenhum novo cliente ativo.",
    )


class BillingForm(forms.Form):
    """Manual subscription metadata, never payment credentials."""

    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    plan_band = forms.CharField(label="Faixa contratada", max_length=64, required=False)
    status = forms.ChoiceField(label="Situação", choices=BillingStatus.choices)
    next_invoice_on = forms.DateField(
        label="Próxima cobrança", required=False, widget=DateInput()
    )
    active_client_limit = forms.IntegerField(
        label="Limite de clientes ativos",
        min_value=0,
        required=False,
        help_text="Em branco: sem limite. A cobrança é feita fora do produto.",
    )


class ProfileForm(forms.Form):
    """Public firm branding with the existing PNG and contrast validation."""

    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    name = forms.CharField(label="Nome da firma", max_length=255)
    brand_name = forms.CharField(
        label="Nome exibido no portal", max_length=120, required=False
    )
    brand_color = forms.CharField(
        label="Cor do portal",
        max_length=7,
        required=False,
        help_text="Use #RRGGBB com contraste suficiente sobre branco.",
    )
    logo = forms.FileField(
        label="Logotipo",
        required=False,
        help_text="PNG de até 200 KiB.",
    )
    remove_logo = forms.BooleanField(label="Remover logotipo atual", required=False)

    def clean_logo(self) -> UploadedFile | None:
        """Validate before materializing the small upload into immutable bytes."""
        upload: UploadedFile | None = self.cleaned_data.get("logo")
        if upload is not None:
            validate_logo(upload)
            upload.seek(0)
        return upload


class NotificationForm(forms.Form):
    """A member may only change their own daily summary opt-in."""

    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    daily_summary = forms.BooleanField(
        label="Receber resumo diário por e-mail",
        required=False,
        help_text="Somente a sua carteira. Você pode desativar a qualquer momento.",
    )


class CancellationForm(forms.Form):
    """The owner chooses an inclusive end date and explicitly confirms it."""

    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    ends_on = forms.DateField(label="Último dia de serviço", widget=DateInput())
    confirm = forms.BooleanField(
        label="Confirmo o encerramento e o prazo de 90 dias para exportação."
    )


class HoldForm(forms.Form):
    """A client-scoped hold reuses the existing versioned retention service."""

    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(
        label="Motivo da retenção", max_length=2000, widget=forms.Textarea
    )
