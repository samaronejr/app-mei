"""Common SQL projections let providers share one database round trip."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, NamedTuple
from uuid import UUID

from django.db.models import Model, QuerySet
from django.utils import timezone

from apps.clients.models import ClientCompany

SOURCE_FIELDS: Final = (
    "brief_kind",
    "pk",
    "client_id",
    "client__legal_name",
    "brief_period",
    "brief_owner",
    "brief_owner_name",
    "brief_due",
    "brief_version",
    "brief_observed",
    "brief_target",
    "brief_title",
    "brief_action",
    "brief_service",
    "brief_state",
    "brief_visible",
)


class SourceRow(NamedTuple):
    """Identifiers and display facts, never a persisted briefing projection."""

    kind: str
    subject_ref: UUID
    client_id: UUID
    client_name: str
    period: date | None
    owner_id: UUID | None
    owner_name: str
    due_on: date | None
    fact_version: str
    observed_at: datetime | None
    target_id: UUID | None
    title: str
    next_action: str
    service_kind: str
    work_state: str
    client_visible: bool


type SourceQuery = QuerySet[
    Model,
    tuple[
        str,
        UUID,
        UUID,
        str,
        date | None,
        UUID | None,
        str,
        date | None,
        str,
        datetime | None,
        UUID | None,
        str,
        str,
        str,
        str,
        bool,
    ],
]


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Every provider starts from this already authorized client queryset."""

    clients: QuerySet[ClientCompany]
    now: datetime

    @property
    def today(self) -> date:
        """Evaluate deadlines on the firm's local calendar day."""
        return timezone.localtime(self.now).date()


def project(rows: QuerySet[Model]) -> SourceQuery:
    """Strip per-model ordering and select the same columns for UNION ALL."""
    return rows.order_by().values_list(*SOURCE_FIELDS)
