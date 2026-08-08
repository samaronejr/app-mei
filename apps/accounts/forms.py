"""Forms for the invitation flow.

Every label here is what a reader actually sees, because `partials/pure/field.html`
draws each control out of the `BoundField` and nothing between here and the screen
rewrites a word. `locale/` compiles no catalogue, so `gettext_lazy` hands back its own
msgid: a label declared in English is rendered in English, and Django's BUNDLED
catalogue covering a few of them by coincidence -- `password` arrives as `senha`,
`email address` as `endereco de email` -- is what makes the rest so easy to miss.
The msgids are therefore written in pt-BR, which is the convention this project holds
across its templates. `tests/ui/test_invitation_labels.py` scans both invitation
screens' rendered text and refuses the English ones by name.
"""

from typing import TYPE_CHECKING, Final

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.tenants.models import TenantRole, client_role_choices, firm_role_choices

if TYPE_CHECKING:  # stubs-only: the lazy proxy is a str everywhere but the type system
    from django_stubs_ext import StrOrPromise


# What each portal seat is CALLED, decided here rather than on the model it names.
#
# `TenantRole.CLIENT_OWNER` and `CLIENT_COLLABORATOR` carry English labels, and both
# `Membership.role` and `Invite.role` declare `choices=TenantRole.choices`. Editing a
# label there rewrites serialized field metadata on two fields, and the autodetector
# answers with an `AlterField` -- a migration that changes no column, for a word only a
# human reads. The display name is therefore mapped at the form boundary, where the
# choice list is consumed, and the model keeps the stored vocabulary it is checked
# against. `firm_role_choices` is deliberately NOT mapped: firm-side role names reach
# accountants through the team page and `get_role_display()`, which no form-boundary
# map can reach, so translating half of them here would be the appearance of a fix.
#
# Keyed off the enum rather than off string literals, so a renamed value fails at
# import instead of silently falling through to the English it was written to replace.
CLIENT_ROLE_LABELS: Final[dict[str, "StrOrPromise"]] = {
    TenantRole.CLIENT_OWNER.value: _("Titular do MEI"),
    TenantRole.CLIENT_COLLABORATOR.value: _("Colaborador do MEI"),
}


def portal_role_choices() -> list[tuple[str, str]]:
    """Return the portal seats an invitation may grant, named for the invitee.

    A thin display pass over `client_role_choices()`, which stays the single authority
    on WHICH seats may be offered -- `test_the_portal_choices_agree_with_the_check_
    constraint` holds that list against the CHECK constraint, and a second list here
    would be a second answer to a question the database already decides.

    An unmapped value falls back to its incoming label rather than raising. A seat this
    map has not been told about is a copy gap, and failing the whole invitation form
    over one would take the feature down to avoid showing one English word.
    """
    return [
        (value, str(CLIENT_ROLE_LABELS.get(value, label)))
        for value, label in client_role_choices()
    ]


class InviteIssueForm(forms.Form):
    """Who to invite, and as what."""

    email = forms.EmailField(label=_("email address"))
    role = forms.ChoiceField(label=_("Papel"), choices=firm_role_choices)


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
    role = forms.ChoiceField(label=_("Papel"), choices=portal_role_choices)


class InviteAcceptForm(forms.Form):
    """The credentials a brand-new invitee chooses when redeeming their link."""

    full_name = forms.CharField(
        label=_("Nome completo"),
        max_length=255,
        required=False,
    )
    password1 = forms.CharField(label=_("password"), widget=forms.PasswordInput)
    password2 = forms.CharField(
        label=_("Confirmação da senha"),
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
