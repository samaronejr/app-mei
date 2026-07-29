"""Forms for the invitation flow."""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.tenants.models import firm_role_choices


class InviteIssueForm(forms.Form):
    """Who to invite, and as what."""

    email = forms.EmailField(label=_("email address"))
    role = forms.ChoiceField(label=_("role"), choices=firm_role_choices)


class InviteAcceptForm(forms.Form):
    """The credentials a brand-new invitee chooses when redeeming their link."""

    full_name = forms.CharField(label=_("full name"), max_length=255, required=False)
    password1 = forms.CharField(label=_("password"), widget=forms.PasswordInput)
    password2 = forms.CharField(
        label=_("password confirmation"),
        widget=forms.PasswordInput,
    )

    def clean_password2(self) -> str:
        """Reject a mismatch, then apply the project's password policy."""
        password1 = self.cleaned_data.get("password1", "")
        password2 = self.cleaned_data["password2"]
        if password1 != password2:
            msg = _("The two password fields did not match.")
            raise ValidationError(msg)
        validate_password(password2)
        return str(password2)
