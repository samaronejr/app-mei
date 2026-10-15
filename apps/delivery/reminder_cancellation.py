"""Cancel obsolete pending system reminders even when flags/pause prevent claims."""

from collections.abc import Callable
from datetime import datetime
from typing import Final
from uuid import UUID

from django.conf import settings
from django.db.models import CharField
from django.db.models.expressions import RawSQL

from apps.core.tasks import assert_no_transaction
from apps.core.tenancy import tenant_context
from apps.delivery.contracts import Reply
from apps.delivery.models import MessageState, OutboundMessage
from apps.delivery.policy import enabled_kinds, still_valid
from apps.delivery.services import _control, transition

CANCELLATION_LIMIT: Final = 50


def cancel_obsolete_reminders(
    tid: UUID, *, clock: Callable[[], datetime] | None = None
) -> int:
    """One control/message/authority transaction per row; no quota or network I/O."""
    assert_no_transaction()
    pending = (MessageState.QUEUED, MessageState.TEMPORARY)
    with tenant_context(tid):
        ids = list(
            OutboundMessage.objects.filter(kind="request_reminder", state__in=pending)
            .annotate(
                notice_mode=RawSQL(
                    # Immutable insert-guarded JSON, fixed SQL, no caller input.
                    "convert_from(delivery_outboundmessage.notice_authorization_bytes, "
                    "'UTF8')::jsonb ->> 'mode'",
                    (),
                    output_field=CharField(),
                )
            )
            .filter(notice_mode="automatic")
            .order_by("pk")
            .values_list("pk", flat=True)[:CANCELLATION_LIMIT]
        )
    count = 0
    for identifier in ids:
        with tenant_context(tid):
            control = _control()
            message = (
                OutboundMessage.objects.select_for_update(skip_locked=True)
                .filter(pk=identifier, state__in=pending)
                .first()
            )
            if message is None:
                continue
            if (
                settings.DELIVERY_PAUSED
                or control.paused
                or message.kind not in enabled_kinds(tid)
            ):
                valid, reason = False, "delivery_paused"
            else:
                valid, reason = still_valid(message, clock=clock)
            if not valid:
                transition(message, Reply(MessageState.CANCELLED, reason))
                count += 1
    return count
