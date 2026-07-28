"""How ready a client is, and what is standing in the way.

Kept out of the model so the score and the blocker can be read from one place and
tested without a database round trip per branch. Both take the checklist as a plain
sequence of statuses, which is what makes them ordinary functions rather than queries.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from apps.clients.models.onboarding import OnboardingStatus

FULLY_READY: Final[int] = 100
NOT_STARTED: Final[int] = 0

ECAC_BLOCKER: Final[str] = (
    "A conta gov.br está em nível {level}. O e-CAC aceita apenas prata ou ouro, "
    "então {count} item(ns) da lista não podem ser concluídos por este acesso."
)


@dataclass(frozen=True, slots=True)
class ChecklistEntry:
    """One checklist row, reduced to the three facts readiness depends on."""

    status: str
    requires_ecac: bool = False


def score(entries: Iterable[ChecklistEntry]) -> int:
    """Return the percentage of applicable items that are done.

    Items marked `not_applicable` are excluded from both halves of the fraction
    rather than counted as done: a client with five irrelevant items and one
    outstanding one is not 83% ready, it is 0% ready with a shorter list.

    A client with no applicable items at all scores 0. The vacuous reading — no
    outstanding work, therefore complete — would report a client nobody has assessed
    as ready to file for.
    """
    applicable = [
        entry for entry in entries if entry.status != OnboardingStatus.NOT_APPLICABLE
    ]
    if not applicable:
        return NOT_STARTED
    done = sum(1 for entry in applicable if entry.status == OnboardingStatus.DONE)
    return round(FULLY_READY * done / len(applicable))


def ecac_blocker(
    entries: Iterable[ChecklistEntry],
    trust_level: str,
    below_ecac: frozenset[str],
) -> str | None:
    """Return why e-CAC is out of reach for this client, or None if it is not.

    A hard blocker rather than a deduction from the score: the outstanding items are
    not merely late, they are unreachable until the account is upgraded, and a
    percentage that quietly drifts upward would hide that from whoever is chasing it.
    """
    if trust_level not in below_ecac:
        return None
    stuck = [
        entry
        for entry in entries
        if entry.requires_ecac
        and entry.status not in {OnboardingStatus.DONE, OnboardingStatus.NOT_APPLICABLE}
    ]
    if not stuck:
        return None
    return ECAC_BLOCKER.format(level=trust_level, count=len(stuck))


__all__ = ["ChecklistEntry", "ecac_blocker", "score"]
