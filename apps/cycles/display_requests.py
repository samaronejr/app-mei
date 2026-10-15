"""Join the unique private request companion into the display read."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID

from django.db.models import F, TextField, Value
from django.db.models.functions import Coalesce

from apps.cycles.models import EvidenceRequest

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django_stubs_ext import WithAnnotations

    from apps.cycles.models import ServiceCycle


class RequestFacts(TypedDict):
    """The nullable companion identity and its scalar display fields."""

    display_internal_id: UUID | None
    display_internal_version: int
    display_instructions: str
    display_waiver_reason: str


@dataclass(frozen=True, slots=True)
class RequestPrivate:
    """Private display text; never a RequestInternal row a command can mutate."""

    pk: UUID
    version: int
    instructions: str
    waiver_reason: str


def display_requests(
    cycle: "ServiceCycle",
) -> "tuple[WithAnnotations[EvidenceRequest, RequestFacts], ...]":
    """Read every request with its unique companion and bound obligation type."""
    rows: QuerySet[WithAnnotations[EvidenceRequest, RequestFacts]] = (
        EvidenceRequest.objects.filter(cycle=cycle)
        .select_related("obligation__obligation_type")
        .annotate(
            display_internal_id=F("internals__pk"),
            display_internal_version=Coalesce(F("internals__version"), Value(0)),
            display_instructions=Coalesce(
                F("internals__instructions"), Value(""), output_field=TextField()
            ),
            display_waiver_reason=Coalesce(
                F("internals__waiver_reason"), Value(""), output_field=TextField()
            ),
        )
        .order_by("created_at", "pk")
    )
    return tuple(rows)


def display_internal(
    request: "WithAnnotations[EvidenceRequest, RequestFacts]",
) -> RequestPrivate | None:
    """Preserve the absent-companion distinction while rendering joined text."""
    if request.display_internal_id is None:
        return None
    return RequestPrivate(
        request.display_internal_id,
        request.display_internal_version,
        request.display_instructions,
        request.display_waiver_reason,
    )
