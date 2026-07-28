"""The scheduler's dead-man's switch.

The beat process fails silently by construction. When it stops, no task raises, no
obligation is generated, and nothing reaches Sentry — because nothing failed. There
is no error to catch and no exception to report; there is only an absence of work,
and absence produces no signal.

So the scheduler is required to assert its own liveness on a timer, and the liveness
probe alarms on the silence. The threshold is deliberately three beat intervals: one
would page somebody on every redeploy and every slow tick, and an alarm nobody trusts
is indistinguishable from no alarm at all.
"""

from datetime import timedelta

from django.utils import timezone

from apps.obligations.models import SchedulerHeartbeat

SCHEDULER_HEARTBEAT_NAME = "beat"
HEARTBEAT_INTERVAL = timedelta(minutes=5)
HEARTBEAT_STALE_AFTER = timedelta(minutes=15)


def record_heartbeat() -> None:
    """Assert that the scheduler is still running.

    `update_or_create` keyed on the name, so redeploys and multiple beat restarts
    move one row rather than accumulating rows nobody reads. `updated_at` is
    `auto_now`, so saving is what stamps it.
    """
    heartbeat, created = SchedulerHeartbeat.objects.get_or_create(
        name=SCHEDULER_HEARTBEAT_NAME,
    )
    if not created:
        heartbeat.save(update_fields=["updated_at"])


def scheduler_is_alive() -> bool:
    """Report whether the scheduler has checked in recently enough.

    A missing row answers False rather than True. "Beat has never started" is
    precisely the condition being watched for, so treating absence as acceptable
    would make the switch mute in the one case it exists to catch.
    """
    newest = SchedulerHeartbeat.objects.order_by("-updated_at").first()
    if newest is None:
        return False
    return timezone.now() - newest.updated_at <= HEARTBEAT_STALE_AFTER
