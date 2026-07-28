"""Resolving a fiscal parameter for a date, and refusing to guess when none covers it.

Two rules, and both are chosen rather than incidental.

**Latest wins.** Among the rows covering a date, the one with the greatest
`valid_from` is the answer. Superseding a value is therefore a pure INSERT — no
close-then-insert dance, no constraint to work around, and no deploy on the day a
ceiling changes.

**No defaults, ever.** A date no row covers raises. The tempting alternative is to
fall back to the newest row regardless of date, which would measure a 2025 competence
against a 2026 ceiling and report the resulting percentage with total confidence. A
number that is quietly wrong is worse here than no number at all, because the whole
point of the monitor is that the accountant stops checking manually.
"""

from datetime import date
from decimal import Decimal

from django.db import models

from apps.fiscal.mei import MEICategory
from apps.obligations.models import FiscalParameter


# Named without an Error suffix on purpose: this is the name the plan's acceptance
# criteria refer to, and it reads correctly at the call site as
# `raise NoEffectiveParameter(...)`.
class NoEffectiveParameter(LookupError):  # noqa: N818
    """Raised when no parameter row covers the requested key, date, and category.

    Deliberately not a subclass of anything the caller is likely to catch broadly.
    Every call site either has a parameter or has nothing useful to say, so this
    propagates to the operator rather than being absorbed into a plausible default.
    """


def effective_parameter(
    key: str,
    on_date: date,
    mei_category: str | None = None,
) -> FiscalParameter:
    """Return the parameter row in force for `key` on `on_date`.

    A category-specific row wins over a category-agnostic one; when the caller names
    no category, the common MEI regime is assumed because that is also
    `ClientCompany.mei_category`'s default. The fallback is deliberately downward:
    guessing the caminhoneiro ceiling for an unclassified client would report an
    over-limit client as compliant, which is the expensive direction to be wrong in.
    """
    category = mei_category or MEICategory.COMMON
    covering = (
        FiscalParameter.objects.filter(key=key, valid_from__lte=on_date)
        .filter(models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=on_date))
        .order_by("-valid_from")
    )
    row = covering.filter(mei_category=category).first()
    if row is None:
        row = covering.filter(mei_category__isnull=True).first()
    if row is None:
        msg = (
            f"No fiscal parameter {key!r} is in force on {on_date.isoformat()} for "
            f"category {category!r}. Seed one rather than assuming a default: a "
            f"silently defaulted limit produces a plausible, wrong percentage."
        )
        raise NoEffectiveParameter(msg)
    return row


def parameter_for(
    key: str,
    on_date: date,
    mei_category: str | None = None,
) -> Decimal:
    """Return the value in force for `key` on `on_date`, as a Decimal.

    Decimal and never float. Every consumer of this number is money or a percentage
    of money, and binary floating point cannot represent 0.20 — a comparison against
    a 20% tolerance band decides whether a client is desenquadrado retroactively to
    January or to the following year.
    """
    return Decimal(effective_parameter(key, on_date, mei_category).value)
