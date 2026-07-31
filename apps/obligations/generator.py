"""Producing a client's rolling DAS calendar, twice if necessary.

**Idempotency is a database constraint here, and the insert is written to survive
hitting it.** `task_acks_late = True` means a task whose worker dies mid-flight is
redelivered and runs again, so duplicate generation is an expected input rather than
a hypothetical. A generator that merely checked first would still lose the race
between two redelivered workers, both of which look and both of which find nothing.

**And the violation must not poison the transaction.** A raw `IntegrityError` inside
the task's atomic block aborts the WHOLE transaction, so every statement after it
fails with `InFailedSqlTransaction` and the retry ends loudly instead of being the
no-op the design depends on. `ignore_conflicts=True` pushes the resolution into
PostgreSQL's `ON CONFLICT DO NOTHING`, where no error is ever raised to abort.
"""

from datetime import date

from django.db import models

from apps.clients.models import ClientCompany
from apps.obligations.calendar import due_date_for
from apps.obligations.models import Obligation, ObligationType
from apps.obligations.rules import DueRuleCache

DAS_CODE = "DAS"
CALENDAR_MONTHS = 12


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _months_from(start: date, count: int) -> list[date]:
    months = []
    year, month = start.year, start.month
    for _step in range(count):
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)  # noqa: PLR2004
    return months


def first_competence_for(client: ClientCompany, window_start: date) -> date:
    """Return the first month this client can owe a DAS for.

    A client that opened inside the window starts at its opening month, never at the
    window's. Generating obligations for months before the company existed would put
    overdue items on an accountant's queue for a client that had not opened yet, and
    every one of them would look real.

    A client with no recorded opening date starts at the window. Refusing to generate
    would be the worse failure: an incomplete registry would silently mean no
    deadlines at all, which is the answer this product must never produce by accident.
    """
    window = _month_start(window_start)
    if client.opened_on is None:
        return window
    return max(window, _month_start(client.opened_on))


def generate_das_calendar(
    client: ClientCompany,
    *,
    starting_from: date,
    rules: DueRuleCache | None = None,
) -> int:
    """Create this client's next twelve DAS obligations, and return how many are new.

    Must be called inside the client's own tenant context: the rows are tenant-scoped
    and the row-level-security policy would reject the insert otherwise.

    `rules` exists so a portfolio-wide sweep can share one resolver across every
    client rather than re-reading the same statute per client. Omitted, one cache
    serves this call's twelve competences, which is still one rule read rather than
    twelve. The eras are platform reference data with no tenant dimension, so a cache
    handed in from outside `each_tenant()` cannot carry one firm's rows into another.
    """
    cache = rules if rules is not None else DueRuleCache()
    das = ObligationType.objects.get(pk=DAS_CODE)
    first = first_competence_for(client, starting_from)
    months = _months_from(first, CALENDAR_MONTHS)

    rows = []
    for competence in months:
        due = due_date_for(das, competence, rules=cache)
        rows.append(
            Obligation(
                tenant_id=client.tenant_id,
                client=client,
                obligation_type=das,
                competence_month=competence,
                nominal_due_date=due.nominal,
                resolved_due_date=due.resolved,
            ),
        )

    existing = _existing_count(client, das, months)
    Obligation.objects.bulk_create(rows, ignore_conflicts=True)
    return len(months) - existing


def _existing_count(
    client: ClientCompany,
    obligation_type: ObligationType,
    months: list[date],
) -> int:
    # Counted before the insert rather than derived from bulk_create's return value:
    # with ignore_conflicts the database returns nothing to distinguish an inserted
    # row from a skipped one, and these primary keys are generated client-side, so
    # every returned object carries an id whether or not it was written.
    return Obligation.objects.filter(
        models.Q(client=client)
        & models.Q(obligation_type=obligation_type)
        & models.Q(competence_month__in=months),
    ).count()
