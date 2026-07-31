"""Drop the due-rule column the era table replaced.

**This migration is ONE-WAY, and deliberately so.** Its reverse re-adds
`due_rule jsonb NOT NULL` with no default onto a table that already holds rows, which
PostgreSQL rejects outright — there is no value it could put in the existing rows.
Django will happily generate that reverse and it will fail at the first `ALTER TABLE`.

The consequence is a rule, not a caveat: **no test and no runbook step may run a
backward `migrate obligations …` past this point.** `0015`'s reverse is exercised by
calling the function directly for exactly this reason, and the deploy runbook rolls
back by image rather than by migration for the release that ships this.

Rolling forward is safe because nothing reads the column any more. `0015` copied every
rule into `ObligationDueRule` as an open-ended era, `calendar.due_date_for` resolves
through `rules.DueRuleCache`, and `0003`'s live-registry helper stopped writing it.
What remains here is a column the ORM would keep sending on every INSERT and that a
future reader would reasonably mistake for the source of truth — two spellings of the
same statutory rule, one edit away from disagreeing.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("obligations", "0015_seed_due_rule_eras")]

    operations = [
        migrations.RemoveField(model_name="obligationtype", name="due_rule"),
    ]
