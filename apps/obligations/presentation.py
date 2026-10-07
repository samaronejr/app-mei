"""How an observation row becomes a renderable state (T17).

One adapter, three surfaces: the portal's per-obligation label, the staff
coverage panel, and the render-policy test all ask the same question — "what do
these source columns mean for the person looking at it?" — so the mapping is a
function rather than a ladder of template `if`s that can drift apart. The
portal template keeps owning its pt-BR wording; this module answers only the
state name.

The rule the mutation test exists to keep honest: a `staff_recorded` fact maps
to `manual`, never to `verified`. Verification is a source property — an
official source produced it — and a person writing a row cannot become one.
"""

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from django.utils import timezone

if TYPE_CHECKING:
    from apps.obligations.models import SourceObservation

MANUAL_SOURCES: Final = frozenset(
    {"staff_recorded", "client_submitted", "legacy_status"}
)
OFFICIAL_SOURCES: Final = frozenset({"official_api", "official_file_import"})
OFFICIAL_FRESHNESS: Final = timedelta(days=7)

# States the adapter may return. `manual` covers every people-sourced claim,
# including a submitted receipt still awaiting review — informed but
# unverified, which is exactly what "manual" says.
PAYMENT_STATES: Final = frozenset({"verified", "manual", "stale", "unknown"})


def payment_state(
    source_kind: str,
    verification_status: str,
    outcome: str,
    *,
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """Classify one payment observation for rendering.

    A rejection or retraction is asserted knowledge about a claim — but the
    knowledge it asserts is "this proves nothing", which renders as unknown,
    not as a special visual.
    """
    if outcome in {"rejected", "retracted"} or verification_status == "unsupported":
        return "unknown"
    if (
        verification_status in {"stale", "refresh_failed"}
        or outcome == "refresh_failed"
    ) and source_kind in MANUAL_SOURCES | OFFICIAL_SOURCES:
        return "stale"
    if source_kind in MANUAL_SOURCES:
        return "manual"
    if source_kind in OFFICIAL_SOURCES and verification_status == "verified":
        if (
            source_kind == "official_api"
            and observed_at is not None
            and (now or timezone.now()) >= observed_at + OFFICIAL_FRESHNESS
        ):
            return "stale"
        return "verified"
    return "unknown"


def current_observation(
    rows: Iterable["SourceObservation"],
    *,
    subject_id: UUID,
    period: date,
    kind: str,
) -> "SourceObservation | None":
    """Select one scoped, live claim without letting a separate attempt erase it.

    Null periods are the legacy spelling of this subject's competence. Filter
    identity before following supersession so an unrelated kind/period cannot
    hide a valid claim. Pending receipts and failed refreshes do not supersede
    independent knowledge; explicit successors still replace their own parent.
    """
    scoped = [
        row
        for row in rows
        if row.subject_ref_id == subject_id
        and row.kind == kind
        and row.period in {period, None}
    ]
    superseded = {row.supersedes_id for row in scoped if row.supersedes_id}
    heads = [row for row in scoped if row.pk not in superseded]
    usable = [
        row
        for row in heads
        if row.outcome not in {"rejected", "retracted"}
        and row.verification_status != "unsupported"
    ]
    claims = [
        row
        for row in usable
        if row.outcome != "refresh_failed"
        and row.verification_status != "refresh_failed"
        and not (row.source_kind == "client_submitted" and row.outcome == "submitted")
    ]
    return max(
        claims or usable or heads,
        key=lambda row: (row.observed_at, row.recorded_at, row.pk),
        default=None,
    )
