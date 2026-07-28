"""The national calendar the resolver consults, and how the movable feasts move.

**The movable feasts are computed, never hardcoded.** Carnaval, Sexta-feira Santa and
Corpus Christi are fixed offsets from Easter, and Easter itself wanders across a
35-day window. A literal table of dates is correct until the January nobody remembers
to refill it, and then every DAS deadline in the year is silently one to three days
early — a failure with no error, no exception, and no symptom until a client is
charged interest.

**Carnaval and Corpus Christi are ponto facultativo, not statutory holidays, and they
are in this table anyway.** What the resolver actually needs to know is whether a
payment can be settled, and banks do not operate on either day. The DAS is a payment,
so the banking calendar is the correct calendar; treating those days as ordinary
business days would place a deadline on a day nobody can pay.

**Scope is honoured on lookup.** Municipal holidays have no rows yet, but the column
exists so they need no migration later. A lookup that ignored scope would let one
city's founding anniversary move the DAS deadline for every client in the country.
"""

from collections.abc import Collection
from datetime import date, timedelta

from django.db import models
from django.utils.translation import gettext_lazy as _

SATURDAY = 5

# Offsets from Easter Sunday. Carnaval is the Monday and Tuesday before Ash
# Wednesday (Easter - 46), Sexta-feira Santa is the Friday before Easter, and
# Corpus Christi is the Thursday sixty days after it.
MOVABLE_FEASTS = (
    (-48, "Carnaval (segunda-feira)"),
    (-47, "Carnaval (terça-feira)"),
    (-2, "Sexta-feira Santa"),
    (60, "Corpus Christi"),
)

# month, day, name. Basis: Lei 662/1949, plus Lei 6.802/1980 for Aparecida and Lei
# 14.759/2024, which made 20 November a national holiday rather than a state one.
FIXED_HOLIDAYS = (
    (1, 1, "Confraternização Universal"),
    (4, 21, "Tiradentes"),
    (5, 1, "Dia do Trabalho"),
    (9, 7, "Independência do Brasil"),
    (10, 12, "Nossa Senhora Aparecida"),
    (11, 2, "Finados"),
    (11, 15, "Proclamação da República"),
    (11, 20, "Dia Nacional de Zumbi e da Consciência Negra"),
    (12, 25, "Natal"),
)


class HolidayScope(models.TextChoices):
    """Whose calendar a holiday belongs to."""

    NATIONAL = "national", _("national")
    MUNICIPAL = "municipal", _("municipal")


def easter_sunday(year: int) -> date:
    """Return Easter Sunday for `year` in the Gregorian calendar.

    The anonymous Gregorian algorithm. The arithmetic is opaque by nature — it
    encodes the metonic cycle plus the solar and lunar corrections — so its
    correctness is established by the test against published dates spanning four
    centuries rather than by reading it.
    """
    golden_number = year % 19
    century, year_of_century = divmod(year, 100)
    century_leap, century_remainder = divmod(century, 4)
    lunar_correction = (century + 8) // 25
    solar_correction = (century - lunar_correction + 1) // 3
    epact = (19 * golden_number + century - century_leap - solar_correction + 15) % 30
    leap_of_century, year_remainder = divmod(year_of_century, 4)
    weekday_offset = (
        32 + 2 * century_remainder + 2 * leap_of_century - epact - year_remainder
    ) % 7
    correction = (golden_number + 11 * epact + 22 * weekday_offset) // 451
    month, day_offset = divmod(epact + weekday_offset - 7 * correction + 114, 31)
    return date(year, month, day_offset + 1)


def national_holidays(year: int) -> tuple[tuple[date, str], ...]:
    """Return every national holiday in `year`, fixed and movable, sorted by date."""
    easter = easter_sunday(year)
    movable = [
        (easter + timedelta(days=offset), name) for offset, name in MOVABLE_FEASTS
    ]
    fixed = [(date(year, month, day), name) for month, day, name in FIXED_HOLIDAYS]
    return tuple(sorted(movable + fixed))


def is_business_day(
    day: date,
    scopes: Collection[str] = (HolidayScope.NATIONAL,),
) -> bool:
    """Report whether `day` is a day on which a payment can actually be settled.

    Deliberately a database read rather than a call to `national_holidays`. The table
    is what a future municipal holiday, or a one-off decreed by the government, gets
    written into — and a resolver that recomputed from code would ignore every one of
    them while looking entirely correct.
    """
    from apps.obligations.models import Holiday  # noqa: PLC0415

    if day.weekday() >= SATURDAY:
        return False
    return not Holiday.objects.filter(date=day, scope__in=list(scopes)).exists()
