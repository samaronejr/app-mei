"""Admin registration for the tenancy root. T-020 refines permissions and scoping."""

from django.contrib import admin

from apps.tenants.models import Invite, Membership, Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin[Tenant]):
    """Browse firms by the subdomain they are reached at."""

    list_display = ("slug", "name", "plan", "is_active")
    list_filter = ("plan", "is_active")
    search_fields = ("slug", "name")


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin[Membership]):
    """Browse who holds which role in which firm."""

    list_display = ("user", "tenant", "role", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("user__email", "tenant__slug")


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin[Invite]):
    """Browse pending invitations.

    `token` is excluded everywhere: the column holds a digest, but showing it still
    leaks a value that is enough to correlate an invitation across systems.
    """

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
