"""Scheduled maintenance of the access-log stream."""

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.audit.models import AccessLog


@shared_task
def purge_access_logs() -> int:
    """Delete access records past the Marco Civil retention period.

    Retention here is a ceiling, not a floor: the statutory duty is to keep six
    months, and keeping longer turns a compliance obligation into a standing
    liability. Business audit events are a different regime and are never purged —
    see `docs/retention.md`.
    """
    cutoff = timezone.now() - timedelta(days=settings.ACCESS_LOG_RETENTION_DAYS)
    # PLATFORM_QUERY_OK: deliberately platform-wide. A retention sweep scoped to
    # one firm would leave every other firm's records past their lawful period.
    deleted, _by_model = AccessLog.objects.filter(created_at__lt=cutoff).delete()
    return int(deleted)
