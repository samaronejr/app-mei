"""Versioned personal preferences with live authority checks at every boundary."""

from collections.abc import Mapping
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404

from apps.accounts.models import User
from apps.audit.models import AuditAction
from apps.audit.services import ObjectRef, record_event
from apps.briefing.forms import parse_selection, selection_values
from apps.briefing.types import BriefingFilters, BriefingScope
from apps.clients.maintenance import load_client
from apps.core.flags import tenant_has
from apps.cycles.models import SavedView
from apps.cycles.services.cycle import check_version, require_actor, tenant_id
from apps.tenants.models import Membership

NAME_LIMIT = 80


def _authorize(actor: User) -> None:
    if not tenant_has(tenant_id(), "briefing_r1"):
        raise Http404
    require_actor(actor, "clients.view_assigned")


def save_view(
    actor: User,
    name: str,
    filters_json: Mapping[str, str],
    *,
    expected_version: int,
) -> SavedView:
    """Create or update exactly the version the person's form was rendered with."""
    clean_name = name.strip()
    if not clean_name or len(clean_name) > NAME_LIMIT:
        message = "Informe um nome com até 80 caracteres."
        raise ValidationError(message)
    scope, filters = parse_selection(filters_json)
    values = selection_values(scope, filters)
    with transaction.atomic():
        member = (
            Membership.objects.for_user(actor)
            .select_for_update()
            .filter(
                tenant_id=tenant_id(), user=actor, client__isnull=True, is_active=True
            )
            .first()
        )
        if member is None:
            raise PermissionDenied
        _authorize(actor)
        if filters.client_id is not None:
            load_client(actor, tenant_id(), filters.client_id)
        existing = (
            SavedView.objects.select_for_update()
            .filter(user=actor, name=clean_name)
            .first()
        )
        check_version(existing.version if existing is not None else 0, expected_version)
        if existing is None:
            saved = SavedView.objects.create(
                tenant_id=tenant_id(), user=actor, name=clean_name, filters_json=values
            )
        else:
            existing.filters_json = values
            existing.version += 1
            existing.save(update_fields=["filters_json", "version"])
            saved = existing
        record_event(
            action=AuditAction.BRIEFING_VIEW_SAVED,
            actor=actor,
            obj=ObjectRef("SavedView", str(saved.pk)),
            metadata={"version": saved.version},
        )
        return saved


def resolve_view(actor: User, view_id: UUID) -> tuple[BriefingScope, BriefingFilters]:
    """Read only this person's filters; briefing re-evaluates their client scope."""
    _authorize(actor)
    saved = get_object_or_404(SavedView.objects.filter(user=actor), pk=view_id)
    raw = saved.filters_json
    message = "A visualização contém filtros inválidos. Salve-a novamente."
    if not isinstance(raw, dict):
        raise ValidationError(message)
    values: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationError(message)
        values[key] = value
    return parse_selection(values)


def delete_view(actor: User, view_id: UUID, *, expected_version: int) -> None:
    """Delete only the currently authorized person's exact saved-view version."""
    with transaction.atomic():
        saved = get_object_or_404(
            SavedView.objects.select_for_update().filter(user=actor), pk=view_id
        )
        check_version(saved.version, expected_version)
        _authorize(actor)
        reference = ObjectRef("SavedView", str(saved.pk))
        version = saved.version
        saved.delete()
        record_event(
            action=AuditAction.BRIEFING_VIEW_DELETED,
            actor=actor,
            obj=reference,
            metadata={"version": version},
        )
