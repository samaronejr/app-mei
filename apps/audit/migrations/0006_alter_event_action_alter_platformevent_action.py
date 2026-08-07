"""Record the two membership-lifecycle actions in the migration state.

`choices` on a `CharField` is not a database constraint — PostgreSQL sees a plain
`varchar(48)` either way — so this migration emits no SQL. It exists only so that
`makemigrations --check` stays clean after `AuditAction` grew two members.

The field is deconstructed from the enum rather than with the generated list of every
action inlined, and that is deliberate on two counts.

`AuditAction`'s own docstring explains the first: the enum is written complete ahead of
the features that use it precisely so that "later phases add behaviour, not migrations".
Freezing a copy of it here would re-introduce the per-feature choices migration that
design exists to avoid — every future addition would inline the whole list again.

The second is that the generated form pins this file to the scope-fidelity scan in
`tests/scope/test_scope_fidelity.py`, which greps `apps/**` for the names of actions
belonging to phases that must not have arrived yet and keeps an explicit list of the
files allowed to spell them. That list cannot grow from here — it lives in a test — and
it should not have to: a generated echo of the enum carries no implementation, only
strings. Referring to the enum keeps the echo out of the tree entirely.

Nothing is lost by not freezing it. There is no schema to diverge from, and a value
this column already holds stays readable whatever the enum later says.
"""

from django.db import migrations, models

import apps.audit.models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0005_alter_accesslog_id_alter_datasubjectrequest_id_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="action",
            field=models.CharField(
                choices=apps.audit.models.AuditAction.choices,
                max_length=48,
                verbose_name="action",
            ),
        ),
        migrations.AlterField(
            model_name="platformevent",
            name="action",
            field=models.CharField(
                choices=apps.audit.models.AuditAction.choices,
                max_length=48,
                verbose_name="action",
            ),
        ),
    ]
