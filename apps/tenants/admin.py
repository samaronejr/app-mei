"""Admin registration for the tenancy root. T-020 refines permissions and scoping."""

from typing import Any

from django import forms
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from apps.core.templatetags.ptbr import TENANT_ROLE_LABELS, papel
from apps.tenants.models import (
    Invite,
    Membership,
    Tenant,
    TenantRole,
    firm_role_choices,
)

# The console is the one place a role name cannot be reached from a template of ours:
# Django draws `list_display` and the filter sidebar itself, both out of the field's
# own choices, so `get_role_display()` puts the model's English label on the screen
# with nothing in between to intercept it. A display callable answers the column and a
# filter of our own answers the sidebar, and both read the same map every other surface
# reads -- `papel` IS that lookup, imported rather than reimplemented so the console
# cannot drift into a fourth vocabulary.


def _firm_role_display_choices() -> list[tuple[str, str]]:
    """Rename what `firm_role_choices` offers without changing which values it offers.

    A twin of `apps.accounts.forms.firm_role_display_choices` rather than an import of
    it: the tenancy root is the layer `apps.accounts` is built on, and reaching up into
    a form module for three words would invert that for no gain. Both read the one map
    at `apps.core.templatetags.ptbr`, which is what keeps them from drifting.
    """
    return [
        (value, str(TENANT_ROLE_LABELS.get(value, label)))
        for value, label in firm_role_choices()
    ]


class RoleListFilter(admin.SimpleListFilter):
    """Offer the sidebar the same role names the rest of the product uses.

    `list_filter = ("role",)` builds its options from `field.flatchoices`, which is the
    enum's English labels, on the same page whose column is already translated. Every
    role the field can hold is offered rather than only those present, which is exactly
    what the field-backed filter did -- narrowing to observed values would silently
    change which filters an operator is offered as rows come and go.
    """

    title = _("Papel")
    parameter_name = "role"

    def lookups(
        self,
        request: HttpRequest,  # noqa: ARG002
        model_admin: admin.ModelAdmin[Any],  # noqa: ARG002
    ) -> list[tuple[str, str]]:
        """Name every role the field accepts, in the order the enum declares them."""
        return [
            (role.value, str(TENANT_ROLE_LABELS[role.value])) for role in TenantRole
        ]

    def queryset(
        self,
        request: HttpRequest,  # noqa: ARG002
        queryset: QuerySet[Any],
    ) -> QuerySet[Any]:
        """Filter on the stored value, never on the word rendered beside it."""
        chosen = self.value()
        return queryset.filter(role=chosen) if chosen else queryset


class InviteAdminForm(forms.ModelForm[Invite]):
    """Offer only the roles an invitation may actually carry.

    An invitation always creates a membership with no client, so a client role would
    violate `invite_role_is_firm_side`. The constraint already rejects it as a field
    error, but offering the choice at all invites an operator to try. This mirrors
    `InviteIssueForm`, which is the same narrowing on the tenant-facing side.

    The choices are renamed rather than reselected: `firm_role_choices` stays the one
    answer to WHICH roles may be offered, and only the word beside each value changes.
    """

    role = forms.ChoiceField(label=_("Papel"), choices=_firm_role_display_choices)

    class Meta:
        """`token` is named nowhere, so the digest cannot reach a rendered form.

        `InviteAdmin.exclude` already drops it; listing the fields positively means
        that stays true if the admin's exclude is ever edited.
        """

        model = Invite
        fields = ("tenant", "email", "role", "expires_at", "accepted_at")


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin[Tenant]):
    """Browse firms by the subdomain they are reached at."""

    list_display = ("slug", "name", "plan", "is_active")
    list_filter = ("plan", "is_active")
    search_fields = ("slug", "name")


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin[Membership]):
    """Browse who holds which role in which firm, and on which side of it.

    `client` is displayed and filterable because it is the only thing distinguishing a
    firm-side membership from a portal identity. Without it two rows for the same user
    and tenant render identically apart from the role string, on the one
    security-critical table that carries no row-level security of its own.

    The role choices are deliberately NOT narrowed here, unlike `InviteAdmin`: both
    kinds of membership are legitimate rows, and the CHECK constraint already rejects a
    mismatched pair as a field error rather than a 500.
    """

    list_display = ("user", "tenant", "client", "role_name", "is_active")
    list_filter = (RoleListFilter, "is_active", "client")
    search_fields = ("user__email", "tenant__slug", "client__legal_name")

    @admin.display(description=_("Papel"), ordering="role")
    def role_name(self, obj: Membership) -> str:
        """Name the role in pt-BR, sorted by the value stored behind it."""
        return papel(obj.role)


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin[Invite]):
    """Browse pending invitations.

    `token` is excluded everywhere: the column holds a digest, but showing it still
    leaks a value that is enough to correlate an invitation across systems.
    """

    form = InviteAdminForm
    list_display = (
        "email",
        "tenant",
        "role_name",
        "expires_at",
        "accepted_at",
    )
    list_filter = (RoleListFilter,)
    search_fields = ("email", "tenant__slug")
    exclude = ("token",)

    @admin.display(description=_("Papel"), ordering="role")
    def role_name(self, obj: Invite) -> str:
        """Name the offered role in pt-BR, sorted by the value stored behind it."""
        return papel(obj.role)
