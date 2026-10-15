"""Versioned deterministic reasons for a daily queue item's priority."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from django.utils import timezone

BRIEFING_RULES_V1: Final = {
    "statutory_due_in_days<=3": 40,
    "request_overdue": 30,
    "client_visible_exception": 20,
    "delivery_failed_or_unknown": 35,
    "approval_waiting_gt_24h": 15,
    "stale_coverage": 10,
    "divergent_document": 35,
}

RULE_LABELS: Final = {
    "statutory_due_in_days<=3": "Prazo legal em até 3 dias ou vencido",
    "request_overdue": "Solicitação atrasada",
    "client_visible_exception": "Pendência visível ao cliente",
    "delivery_failed_or_unknown": "Envio recusado ou resultado incerto",
    "approval_waiting_gt_24h": "Aprovação aguardando há mais de 24 horas",
    "stale_coverage": "Cobertura desatualizada",
    "divergent_document": "Documento de outra empresa",
}

STATUTORY_HORIZON: Final = timedelta(days=3)
APPROVAL_WAIT: Final = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class PriorityFacts:
    """Facts supplied by providers, without guessing source freshness."""

    statutory_due_on: date | None = None
    request_due_on: date | None = None
    client_visible_exception: bool = False
    delivery_problem: bool = False
    approval_requested_at: datetime | None = None
    stale_coverage: bool = False
    divergent_document: bool = False


def priority_factors(
    facts: PriorityFacts, *, now: datetime, today: date | None = None
) -> tuple[tuple[str, int], ...]:
    """Explain every contributing weight against one injected Brasilia clock.

    A read scoring many items may pass ``today``, the local date of ``now``.
    """
    if today is None:
        today = timezone.localtime(now).date()
    applicable = (
        (
            "statutory_due_in_days<=3",
            facts.statutory_due_on is not None
            and facts.statutory_due_on <= today + STATUTORY_HORIZON,
        ),
        (
            "request_overdue",
            facts.request_due_on is not None and facts.request_due_on < today,
        ),
        ("client_visible_exception", facts.client_visible_exception),
        ("delivery_failed_or_unknown", facts.delivery_problem),
        (
            "approval_waiting_gt_24h",
            facts.approval_requested_at is not None
            and now - facts.approval_requested_at > APPROVAL_WAIT,
        ),
        ("stale_coverage", facts.stale_coverage),
        ("divergent_document", facts.divergent_document),
    )
    return tuple(
        (code, BRIEFING_RULES_V1[code]) for code, applies in applicable if applies
    )
