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

The two msgids that coincidence DID cover are written in pt-BR here as well, and that
is a deliberate trade rather than a slip. Both arrived lower-case beside sentence-case
siblings -- `senha` next to `Confirmação da senha` on the same form, `endereço de
email` next to `Papel` -- so the screen an invitee meets before she has an account
disagreed with itself about how a label is written. Writing them here gives up the
bundled translation of those two msgids, and with `locale/` empty there is no project
catalogue to put one back; one casing convention on a first-run screen is worth more
than a translation of two words this file can simply say in Portuguese itself.

`E-mail` is not invented for the occasion: `templates/accounts/team.html` already
writes that word above this address, in the column heading over the pending-invite
table, for exactly the concept this field collects -- the same reason `Papel` was
chosen over anything else for the seat chooser beside it.
"""

from typing import TYPE_CHECKING, Final

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.templatetags.ptbr import TENANT_ROLE_LABELS
from apps.tenants.models import TenantRole, client_role_choices, firm_role_choices

if TYPE_CHECKING:  # stubs-only: the lazy proxy is a str everywhere but the type system
    from django_stubs_ext import StrOrPromise


def _role_labels(*roles: TenantRole) -> dict[str, "StrOrPromise"]:
    """Return the pt-BR name of each role named, read out of the one map that holds it.

    Sliced from `TENANT_ROLE_LABELS` rather than retyped. Two independently written
    lists of these five words agree until one of them is edited, and from then on each
    puts a different word in front of a different reader -- which is the whole defect
    the filter family was introduced to remove, reintroduced one layer up.

    Subscripting rather than `.get()`: a role the central map has not been told about
    is a gap in the source of truth, and it should stop the import rather than be
    papered over with the English label this exists to replace.
    """
    return {role.value: TENANT_ROLE_LABELS[role.value] for role in roles}


# What each seat is CALLED, applied here rather than on the model that names it.
#
# `Membership.role` and `Invite.role` both declare `choices=TenantRole.choices`, so
# editing a label on the enum rewrites serialized field metadata on two fields and the
# autodetector answers with an `AlterField` -- a migration that changes no column, for
# a word only a human reads. The display name is therefore mapped at the form boundary,
# where the choice list is consumed, and the model keeps the stored vocabulary its
# CHECK constraints are written against.
#
# This used to map the portal seats ONLY, on the reasoning that firm-side names also
# reach accountants through `get_role_display()`, which no form-boundary map can reach,
# so translating half of them here would be the appearance of a fix. That reasoning
# held until the `|papel` filter gave those templates their own route to the same
# words: `templates/accounts/team.html` and `templates/accounts/invite_confirm.html`
# now read the role through the filter, and `apps/tenants/admin.py` renders it through
# a display callable, so this map is no longer half a fix -- it is the form's share of
# a whole one.
CLIENT_ROLE_LABELS: Final[dict[str, "StrOrPromise"]] = _role_labels(
    TenantRole.CLIENT_OWNER,
    TenantRole.CLIENT_COLLABORATOR,
)
FIRM_ROLE_LABELS: Final[dict[str, "StrOrPromise"]] = _role_labels(
    TenantRole.OWNER,
    TenantRole.STAFF_ACCOUNTANT,
    TenantRole.OPERATIONS_ADMIN,
)


def _named(
    choices: list[tuple[str, str]],
    labels: dict[str, "StrOrPromise"],
) -> list[tuple[str, str]]:
    """Rename an offered choice list without changing WHICH values it offers.

    An unmapped value falls back to its incoming label rather than raising. A seat a
    map has not been told about is a copy gap, and failing the whole invitation form
    over one would take the feature down to avoid showing one English word.
    """
    return [(value, str(labels.get(value, label))) for value, label in choices]


def portal_role_choices() -> list[tuple[str, str]]:
    """Return the portal seats an invitation may grant, named for the invitee.

    A thin display pass over `client_role_choices()`, which stays the single authority
    on WHICH seats may be offered -- `test_the_portal_choices_agree_with_the_check_
    constraint` holds that list against the CHECK constraint, and a second list here
    would be a second answer to a question the database already decides.
    """
    return _named(client_role_choices(), CLIENT_ROLE_LABELS)


def firm_role_display_choices() -> list[tuple[str, str]]:
    """Return the firm-side seats an invitation may grant, named for the owner.

    The twin of `portal_role_choices`, and deliberately built the same way: over
    `firm_role_choices()` rather than in place of it, so the question of which roles a
    firm-side invitation may carry keeps exactly one answer -- the one
    `invite_role_is_firm_side` is written against.
    """
    return _named(firm_role_choices(), FIRM_ROLE_LABELS)


class InviteIssueForm(forms.Form):
    """Who to invite, and as what."""

    email = forms.EmailField(label=_("E-mail"))
    role = forms.ChoiceField(label=_("Papel"), choices=firm_role_display_choices)


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

    email = forms.EmailField(label=_("E-mail"))
    role = forms.ChoiceField(label=_("Papel"), choices=portal_role_choices)


class InviteAcceptForm(forms.Form):
    """The credentials a brand-new invitee chooses when redeeming their link."""

    full_name = forms.CharField(
        label=_("Nome completo"),
        max_length=255,
        required=False,
    )
    password1 = forms.CharField(label=_("Senha"), widget=forms.PasswordInput)
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
