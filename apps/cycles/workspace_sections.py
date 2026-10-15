"""The request section of the client-period workspace (AXR1-043).

Read-only, for a client the caller already resolved through `load_client`, so the
portfolio decision is made once for the page. One query lists every request of the
competence with its cycle, the obligation a receipt pays for and whether the client
has sent a file that counts, so the cost does not grow with the number of rows.
Firm-only request data (`RequestInternal`) is not read here.
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.clients.models import ClientCompany
from apps.cycles.evidence import EvidenceRequest, counted_links
from apps.cycles.services.requests import client_status


@dataclass(frozen=True, slots=True)
class PeriodRequest:
    """One request of the competence as the workspace lists it.

    `client_status` is the chip `client_status()` derives (`recebido`, `atrasada`
    or the stored state); it is never stored. `obligation_name` is empty for a
    document request, which binds no obligation.
    """

    request_id: UUID
    cycle_id: UUID
    cycle_label: str
    title: str
    kind: str
    kind_label: str
    state: str
    state_label: str
    client_status: str
    due_on: date | None
    requested_at: datetime | None
    obligation_name: str
    obligation_month: date | None


def project_request(
    row: EvidenceRequest, *, today: date, submitted: bool
) -> PeriodRequest:
    """Project one already-joined request row without another query."""
    obligation = row.obligation
    return PeriodRequest(
        request_id=row.pk,
        cycle_id=row.cycle_id,
        cycle_label=row.cycle.playbook.name,
        title=row.title,
        kind=row.kind,
        kind_label=row.get_kind_display(),
        state=row.state,
        state_label=row.get_state_display(),
        client_status=client_status(row, submitted=submitted, today=today),
        due_on=row.due_on,
        requested_at=row.requested_at,
        obligation_name=obligation.obligation_type.name if obligation else "",
        obligation_month=obligation.competence_month if obligation else None,
    )


def period_requests(client: ClientCompany, period: date) -> list[PeriodRequest]:
    """List the competence's requests across its cycles, oldest first, in one query."""
    today = timezone.localdate()
    rows = (
        EvidenceRequest.objects.filter(client=client, cycle__period_start=period)
        .select_related("cycle__playbook", "obligation__obligation_type")
        # The same "sent" rule the reminder command applies: a refused file, a
        # document of another company or a rejected file does not count.
        .annotate(submitted=Exists(counted_links().filter(request_id=OuterRef("pk"))))
        .order_by("created_at", "pk")
    )
    return [
        project_request(row, today=today, submitted=bool(row.__dict__.get("submitted")))
        for row in rows
    ]
