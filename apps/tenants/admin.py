"""Admin registration for the tenancy root. T-020 refines permissions and scoping."""

from typing import Any

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import URLPattern, path
from django.utils.translation import gettext_lazy as _

from apps.audit.models import AuditAction
from apps.audit.services import Origin, record_platform_event
from apps.core.flags import FEATURES
from apps.core.netaddr import client_ip
from apps.core.templatetags.ptbr import TENANT_ROLE_LABELS, papel
from apps.tenants.authority import lock_users
from apps.tenants.models import (
    Invite,
    Membership,
    Tenant,
    TenantRole,
    firm_role_choices,
)
from apps.tenants.operator_views import billing_view, new_firm_view

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


class TenantAdminForm(forms.ModelForm[Tenant]):
    """Hold the tenant form to the flag registry the rollout contract defines.

    `features` is an array of free-text slugs at the column level on purpose — a
    new flag is a code change, not a migration — so the admin form is where an
    unknown slug is refused. Storing one would look like a grant and answer every
    gate with `False` forever, which is the silent shape `UnknownFeature` exists
    to prevent at the code layer; here it becomes a field error an operator can
    actually see and fix.
    """

    version = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def get_initial_for_field(self, field: forms.Field, field_name: str) -> object:
        """Render the revision without exposing the non-editable model field."""
        if field_name == "version":
            return self.instance.settings_version
        return super().get_initial_for_field(field, field_name)

    @transaction.atomic
    def clean(self) -> dict[str, object]:
        """Fence legacy operator changes before a profile can overwrite them."""
        cleaned = super().clean() or {}
        previous = (
            # PLATFORM_QUERY_OK: admin already authorized this exact root editor.
            Tenant.objects.select_for_update(no_key=True)
            .filter(pk=self.instance.pk)
            .first()
        )
        if previous is not None:
            if (
                "firm_settings_r1" in (*previous.features, *cleaned.get("features", []))
                and cleaned.get("version") != previous.settings_version
            ):
                msg = "As configurações mudaram. Recarregue antes de salvar."
                raise ValidationError(msg)
            self.instance.settings_version = previous.settings_version
        return cleaned

    class Meta:
        """Fields listed positively: the entitlement pair is why this form exists."""

        model = Tenant
        fields = (
            "name",
            "slug",
            "plan",
            "is_active",
            "features",
            "active_client_limit",
        )

    def clean_features(self) -> list[str]:
        """Refuse slugs `apps.core.flags.FEATURES` does not define."""
        values: list[str] = self.cleaned_data["features"]
        unknown = sorted(set(values) - FEATURES)
        if unknown:
            raise ValidationError(
                _("Unknown feature flags: %(flags)s."),
                params={"flags": ", ".join(unknown)},
            )
        return values


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin[Tenant]):
    """Browse firms by the subdomain they are reached at.

    `features` and `active_client_limit` are editable here because that is how a
    firm joins a rollout cohort — the entitlement is data, and the console is the
    operator surface that writes it. Every save is recorded as a `PlatformEvent`
    naming the actor, the firm and the changed fields, so a flag appearing on a
    tenant is never an untracked event.
    """

    form = TenantAdminForm
    list_display = ("slug", "name", "plan", "is_active")
    list_filter = ("plan", "is_active")
    search_fields = ("slug", "name")

    def get_urls(self) -> list[URLPattern]:
        """Put audited onboarding and manual billing ahead of the model catch-all."""
        return [
            path(
                "nova-firma/",
                self.admin_site.admin_view(new_firm_view),
                name="tenants_tenant_new_firm",
            ),
            path(
                "<uuid:tenant_id>/cobranca/",
                self.admin_site.admin_view(billing_view),
                name="tenants_tenant_billing",
            ),
            *super().get_urls(),
        ]

    def save_model(
        self,
        request: HttpRequest,
        obj: Tenant,
        form: forms.ModelForm[Tenant],
        change: bool,
    ) -> None:
        """Persist the firm and record the entitlement change as a PlatformEvent.

        A platform event rather than a tenant-scoped Event: `tenants_tenant` is a
        tenancy root with no tenant context of its own, so the row names the firm
        through `tenant_id` and `subject` instead of being written inside it.
        """
        if change:
            obj.settings_version += 1
        super().save_model(request, obj, form, change)
        record_platform_event(
            action=AuditAction.TENANT_UPDATED,
            origin=Origin(
                actor=request.user,
                subject=obj.slug,
                ip=client_ip(request),
            ),
            tenant_id=obj.pk,
            metadata={
                "changed_fields": sorted(form.changed_data),
                "created": not change,
                "features": sorted(obj.features),
                "active_client_limit": obj.active_client_limit,
            },
        )


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

    @transaction.atomic
    def save_model(
        self,
        request: HttpRequest,
        obj: Membership,
        form: forms.ModelForm[Membership],
        change: bool,
    ) -> None:
        """Fence both sides of a user/tenant move before locking the seat."""
        # PLATFORM_QUERY_OK: the platform admin edits this exact root row.
        # CLIENT_SCOPE_OK: the platform editor handles both firm and portal seats.
        previous = Membership.objects.filter(pk=obj.pk).first() if change else None
        users = {obj.user_id}
        if previous is not None:
            users.add(previous.user_id)
        lock_users(users)
        if previous is not None:
            # PLATFORM_QUERY_OK: recheck discovery after acquiring user fences.
            # CLIENT_SCOPE_OK: the same exact firm or portal row is being edited.
            locked = Membership.objects.select_for_update(no_key=True).get(pk=obj.pk)
            if locked.user_id != previous.user_id or locked.version != previous.version:
                msg = "O acesso mudou. Recarregue antes de salvar."
                raise ValidationError(msg)
        super().save_model(request, obj, form, change)

    @transaction.atomic
    def delete_model(self, request: HttpRequest, obj: Membership) -> None:
        """Use the same user fence when deleting one seat."""
        lock_users((obj.user_id,))
        super().delete_model(request, obj)

    @transaction.atomic
    def delete_queryset(
        self, request: HttpRequest, queryset: QuerySet[Membership]
    ) -> None:
        """Fence a platform-selected batch before any member is removed."""
        lock_users(queryset.values_list("user_id", flat=True))
        for member in queryset.order_by("tenant_id", "pk"):
            self.delete_model(request, member)

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
