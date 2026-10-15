"""Admin registration for the audit streams. Everything here is read-only.

The audit tables reject `UPDATE` and `DELETE` at the database for every role including
their owner, so an editable admin form would offer an action that can only ever end in
an exception. Turning the permissions off states the same guarantee one layer up,
where a reviewer reading the admin sees it.
"""

from typing import Any, ClassVar

from django.contrib import admin
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse

from apps.audit.models import AccessLog, DataSubjectRequest, Event, PlatformEvent
from apps.authz.support import current_support, in_support_mode
from apps.core.tenancy import current_tenant_id
from apps.tenants.admin_views import select_tenant_view


class ReadOnlyAdmin(admin.ModelAdmin[Any]):
    """A changelist that can be read and never written."""

    def get_queryset(self, request: HttpRequest) -> QuerySet[Any]:
        """Keep unpartitioned platform audit streams outside a support grant."""
        rows = super().get_queryset(request)
        return rows.none() if in_support_mode() else rows

    def has_add_permission(self, request: HttpRequest) -> bool:  # noqa: ARG002
        """Refuse creation."""
        return False

    def has_change_permission(
        self,
        request: HttpRequest,  # noqa: ARG002
        obj: Any = None,  # noqa: ANN401, ARG002
    ) -> bool:
        """Refuse editing."""
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,  # noqa: ARG002
        obj: Any = None,  # noqa: ANN401, ARG002
    ) -> bool:
        """Refuse deletion."""
        return False


class TenantScopedAdminMixin(admin.ModelAdmin[Any]):
    """Render an explicit empty state instead of a silently empty changelist.

    With no firm selected, both isolation layers correctly return nothing — and "shows
    only the selected firm's rows" is trivially true of zero rows. An operator would
    read that as "this firm has no data". Saying so explicitly is the difference
    between an answer and an accident.

    Declared on `ModelAdmin` rather than on bare `object`, because the else branch is
    a `super().changelist_view` call and a bare-object base leaves it unresolved. Every
    admin this is mixed into already derives from `ModelAdmin`, so the MRO is what it
    always was — the base merely says so.
    """

    def changelist_view(
        self,
        request: HttpRequest,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Divert to the selector when no firm is in context."""
        if current_tenant_id.get() is None:
            return select_tenant_view(request)
        return super().changelist_view(request, extra_context)


@admin.register(Event)
class EventAdmin(TenantScopedAdminMixin, ReadOnlyAdmin):
    """The tenant-scoped audit trail, scoped further by the admin's selected firm."""

    list_display = ("created_at", "action", "actor", "object_type", "object_id")
    list_filter = ("action",)
    search_fields = ("object_id", "object_type")
    date_hierarchy = "created_at"

    def get_queryset(self, request: HttpRequest) -> QuerySet[Event]:  # noqa: ARG002
        """Intersect the firm's audit references with the approved support scope."""
        rows = Event.objects.all()
        support = current_support.get()
        if support is None:
            return rows
        firm_metadata = (
            Q(object_type="Tenant", object_id=str(support.tenant_id))
            | Q(object_type="SupportGrant", object_id=str(support.grant_id))
        ) & ~Q(metadata__has_key="client_id")
        client_ids = list(support.client_ids)
        return rows.filter(
            firm_metadata
            | Q(metadata__client_id__in=client_ids)
            | Q(
                object_type__in=("ClientCompany", "clients.ClientCompany"),
                object_id__in=client_ids,
            )
        )


@admin.register(PlatformEvent)
class PlatformEventAdmin(ReadOnlyAdmin):
    """Identity events. Platform level, so no firm needs to be selected."""

    list_display = ("created_at", "action", "actor", "subject", "ip", "tenant")
    list_filter = ("action",)
    search_fields = ("subject",)
    date_hierarchy = "created_at"


@admin.register(AccessLog)
class AccessLogAdmin(ReadOnlyAdmin):
    """The Marco Civil stream. Not editable, and purged on a schedule instead."""

    list_display = ("created_at", "method", "path", "status_code", "user", "tenant")
    list_filter = ("method", "status_code")
    search_fields = ("path",)
    date_hierarchy = "created_at"


@admin.register(DataSubjectRequest)
class DataSubjectRequestAdmin(admin.ModelAdmin[DataSubjectRequest]):
    """Rights requests. Editable, because closing one is part of answering it."""

    list_display = (
        "created_at",
        "request_type",
        "requester_name",
        "resolved_at",
        "encarregado_notified_at",
        "notification_last_error",
    )
    list_filter = ("request_type", "relationship")
    search_fields = ("requester_name", "email")
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "created_at",
        "encarregado_notified_at",
        "notification_last_error",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[DataSubjectRequest]:
        """Rights requests have no grant-scoped client partition."""
        rows = super().get_queryset(request)
        return rows.none() if in_support_mode() else rows
