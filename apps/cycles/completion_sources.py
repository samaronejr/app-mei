"""Discover source subjects from model fields and fence them without lock inversion."""

from uuid import UUID

from django.db import OperationalError
from django.db.models import QuerySet
from psycopg.errors import LockNotAvailable

from apps.cycles.errors import VersionConflict
from apps.cycles.models import ActionProposal, EvidenceRequest, ServiceCycle
from apps.obligations.models import Obligation, SourceObservation


def source_subjects(cycle: ServiceCycle, selected: tuple[UUID, ...] = ()) -> set[UUID]:
    """Collect only subjects bound to this client, period and cycle's own evidence."""
    observations = set(selected)
    for inputs in ActionProposal.objects.filter(cycle=cycle).values_list(
        "input_spec", flat=True
    ):
        observations.update(UUID(value) for value in inputs["observations"])
    subjects = set(
        EvidenceRequest.objects.filter(
            cycle=cycle, obligation__isnull=False
        ).values_list("obligation_id", flat=True)
    )
    subjects.update(
        SourceObservation.objects.filter(
            client_id=cycle.client_id, period=cycle.period_start, pk__in=observations
        ).values_list("subject_ref_id", flat=True)
    )
    return subjects


def scoped_observations(cycle: ServiceCycle) -> QuerySet[SourceObservation]:
    """Preserve the whole supersession history, not a timestamp-only latest row."""
    return SourceObservation.objects.filter(
        client_id=cycle.client_id,
        period=cycle.period_start,
        subject_ref_id__in=source_subjects(cycle),
    )


def lock_index_subjects(cycle: ServiceCycle, selected: tuple[UUID, ...] = ()) -> None:
    """Refuse contention instead of waiting cycle -> subject against a reverse writer.

    SourceObservation's INSERT trigger owns the same subject fence. NOWAIT is
    deliberate: both the old cycle-first and repaired subject-first writer order
    remain safe. A conflict rolls back the caller's transaction with zero effects.
    """
    try:
        list(
            Obligation.objects.select_for_update(nowait=True, no_key=True)
            .filter(client_id=cycle.client_id, pk__in=source_subjects(cycle, selected))
            .order_by("pk")
        )
    except OperationalError as error:
        if not isinstance(error.__cause__, LockNotAvailable):
            raise
        message = "Uma fonte está sendo alterada. Recarregue antes de decidir."
        raise VersionConflict(message) from error
