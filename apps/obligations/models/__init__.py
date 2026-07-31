"""The obligation engine's tables, split by responsibility and re-exported here.

Django discovers models through this package, so every concrete model must be
imported below or its table is never created.
"""

from apps.obligations.models.documents import Document, new_storage_key
from apps.obligations.models.operations import (
    Obligation,
    ObligationStatus,
    SchedulerHeartbeat,
)
from apps.obligations.models.reference import (
    FiscalParameter,
    Holiday,
    ObligationType,
    Periodicity,
)
from apps.obligations.models.revenue import MonthlyRevenue

__all__ = [
    "Document",
    "FiscalParameter",
    "Holiday",
    "MonthlyRevenue",
    "Obligation",
    "ObligationStatus",
    "ObligationType",
    "Periodicity",
    "SchedulerHeartbeat",
    "new_storage_key",
]
