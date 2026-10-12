"""The one implemented automatic rule: today's overdue notice, no catch-up."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.utils import timezone

from apps.delivery.intents import BusinessIntent

if TYPE_CHECKING:
    from apps.cycles.models import ServiceCycle

RULE_VERSION: Final = 1
SYSTEM_OPERATION: Final = "sweep_reminders"
ACKNOWLEDGEMENT: Final = "notice-policy.automatic-reminders.v1"


@dataclass(frozen=True, slots=True)
class ReminderOccurrence:
    """Today's identity plus the fresh clock used after acquiring source locks."""

    on: date
    clock: Callable[[], datetime]


def occurrence_day(now: datetime) -> date:
    """Use the deployment's Brazilian timezone, not a leaked request override."""
    return timezone.localdate(now, timezone.get_default_timezone())


def expires_at(on: date) -> str:
    """Return the occurrence expiry; a later sweep never catches a missed day up."""
    return datetime.combine(
        on + timedelta(days=1), time.min, timezone.get_default_timezone()
    ).isoformat()


def reminder_intent(
    cycle: "ServiceCycle", request_id: UUID, contact_id: UUID, on: date
) -> BusinessIntent:
    """Bind the current cycle; daily uniqueness comes from daily_reminder_key."""
    return BusinessIntent(
        cycle.engagement_id,
        cycle.playbook_id,
        cycle.period_start,
        cycle.period_end,
        cycle.amendment,
        f"automatic_reminder:{request_id}:{contact_id}",
        "request_reminder",
        on.toordinal(),
    )


def daily_reminder_key(request_id: UUID, contact_id: UUID, on: date) -> str:
    """Name one request/contact/local day, stable across approved cycle amendments."""
    return f"automatic_reminder:{request_id}:{contact_id}:{on.toordinal()}"
