"""Materialize the 2026 and 2027 national calendars from the Easter computation.

The dates are computed rather than transcribed, and the computation is imported from
`apps.obligations.holidays` rather than duplicated here. A second literal list in this
file would be a second thing to maintain, and the two would disagree in whichever year
somebody updated only one of them — with no error, because both lists would be full of
perfectly plausible dates.

Two years are seeded because the DAS calendar generator produces a rolling twelve-month
window, so it routinely resolves deadlines that fall in the following year. Extending
the horizon is a re-run of this seed with a wider range, not a code change.
"""

from django.apps import apps as global_apps
from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.obligations.holidays import HolidayScope, national_holidays

SEEDED_YEARS = (2026, 2027)


def seed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Insert both years' national holidays. Idempotent on the unique constraint."""
    holiday = apps.get_model("obligations", "Holiday")
    for year in SEEDED_YEARS:
        for day, name in national_holidays(year):
            holiday.objects.get_or_create(
                date=day,
                scope=HolidayScope.NATIONAL,
                name=name,
                defaults={},
            )


def unseed(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Remove exactly the national rows for the seeded years, and nothing else."""
    holiday = apps.get_model("obligations", "Holiday")
    holiday.objects.filter(
        date__year__in=SEEDED_YEARS,
        scope=HolidayScope.NATIONAL,
    ).delete()


def seed_from_global_registry() -> None:
    """Re-seed using the live app registry, for the test suite's restore fixture.

    An empty holiday table does not raise. It answers "every weekday is a business
    day", which is a valid-looking answer and a wrong one — so a due-date test would
    pass while asserting nothing about the calendar.
    """
    seed(global_apps, None)


class Migration(migrations.Migration):
    dependencies = [("obligations", "0004_national_holidays")]

    operations = [migrations.RunPython(seed, unseed)]
