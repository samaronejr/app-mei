"""Values returned by the briefing read model and accepted by its filter boundary."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from apps.briefing.rules import RULE_LABELS

type BriefingScope = Literal["me", "team"]


@dataclass(frozen=True, slots=True)
class BriefingFilters:
    """Narrowing values only: none of these values grants access to a client."""

    responsible: UUID | None = None
    client_id: UUID | None = None
    service: str = ""
    regime: str = ""
    uf: str = ""
    municipality: str = ""
    period: date | None = None
    work_state: str = ""
    risk_reason: str = ""
    freshness: str = ""


@dataclass(frozen=True, slots=True)
class BriefingItem:
    """One actionable source fact, with its exact dismissal identity."""

    kind: str
    subject_ref: UUID
    client_id: UUID
    period: date | None
    owner_id: UUID | None
    due_on: date | None
    factors: tuple[tuple[str, int], ...]
    source_observed_at: datetime | None
    next_action: str
    evidence_refs: tuple[str, ...]
    fact_version: str
    client_name: str
    owner_name: str
    title: str
    url: str
    service_kind: str
    work_state: str
    freshness: str

    @property
    def id(self) -> str:
        """Keep a total order even when two providers refer to the same UUID."""
        return f"{self.kind}:{self.subject_ref}"

    @property
    def priority(self) -> int:
        """The displayed priority is exactly the sum of its displayed factors."""
        return sum(weight for _, weight in self.factors)

    @property
    def order_key(self) -> tuple[int, date, str]:
        """Sort higher priorities first, undated items last, then a unique key."""
        return -self.priority, self.due_on or date.max, self.id

    @property
    def reasons(self) -> tuple[tuple[str, int], ...]:
        """Render the registered reason labels instead of internal rule codes."""
        return tuple((RULE_LABELS[code], weight) for code, weight in self.factors)


@dataclass(frozen=True, slots=True)
class BriefingCount:
    """A count derived from the same visible facts as the queue."""

    kind: str
    label: str
    total: int


@dataclass(frozen=True, slots=True)
class BriefingChange:
    """A scoped material event without its internal payload."""

    client_name: str
    label: str
    occurred_at: datetime
    url: str


@dataclass(frozen=True, slots=True)
class BriefingPage:
    """One keyset page and the counts before its pagination boundary."""

    items: tuple[BriefingItem, ...]
    counts: tuple[BriefingCount, ...]
    total: int
    next_cursor: str | None
    computed_at: datetime
    changes: tuple[BriefingChange, ...]
    owners: tuple[tuple[UUID, str], ...] = ()
    failed_providers: tuple[str, ...] = ()
