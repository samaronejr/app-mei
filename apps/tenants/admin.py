"""Admin registration for the tenancy root. T-020 refines permissions and scoping."""

from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.tenants.models import Invite, Membership, Tenant, firm_role_choices


class InviteAdminForm(forms.ModelForm[Invite]):
    """Offer only the roles an invitation may actually carry.

    An invitation always creates a membership with no client, so a client role would
    violate `invite_role_is_firm_side`. The constraint already rejects it as a field
    error, but offering the choice at all invites an operator to try. This mirrors
    `InviteIssueForm`, which is the same narrowing on the tenant-facing side.
    """

    role = forms.ChoiceField(label=_("role"), choices=firm_role_choices)

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

    list_display = ("user", "tenant", "client", "role", "is_active")
    list_filter = ("role", "is_active", "client")
    search_fields = ("user__email", "tenant__slug", "client__legal_name")


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
        "role",
        "expires_at",
        "accepted_at",
    )
    list_filter = ("role",)
    search_fields = ("email", "tenant__slug")
    exclude = ("token",)
