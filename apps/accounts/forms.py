"""Forms for the invitation flow."""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.tenants.models import client_role_choices, firm_role_choices


class InviteIssueForm(forms.Form):
    """Who to invite, and as what."""

    email = forms.EmailField(label=_("email address"))
    role = forms.ChoiceField(label=_("role"), choices=firm_role_choices)


class PortalInviteIssueForm(forms.Form):
    """Who to give a portal seat to, and as what.

    A separate form from `InviteIssueForm` rather than one with a swapped choice list.
    The two offer disjoint sets, and `ChoiceField` validation is the only thing standing
    between a firm role posted to this endpoint and an `IntegrityError` raised at ACCEPT
    time — in the invitee's face, days later, with the token already spent.

    Which client the seat is in is NOT a field. It comes from the URL and is resolved
    through the firm's own portfolio lens; accepting it in the body would mean the one
    value that decides whose books are opened arrives from the same place as everything
    else the form distrusts.
    """

    email = forms.EmailField(label=_("email address"))
    role = forms.ChoiceField(label=_("role"), choices=client_role_choices)


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
