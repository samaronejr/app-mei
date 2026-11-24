"""Repair installed 0026 databases as well as fresh historical upgrades.

Classification uses supported producer history. An out-of-band admin or raw
SQL flip and reversal that leaves no event, revision or artifact is outside
the guarantee: it is indistinguishable from an ordinary request history.
"""

from django.db import migrations

from apps.cycles.migrations._request_cutover import install, refuse_exposure


class Migration(migrations.Migration):
    dependencies = [
        ("cycles", "0026_internal_request_lifecycle"),
        ("integrations", "0003_call_protocol"),
    ]

    # Catch up installations which already recorded both original migrations.
    operations = [
        migrations.RunPython(install, migrations.RunPython.noop),
        migrations.RunPython(migrations.RunPython.noop, refuse_exposure),
    ]
