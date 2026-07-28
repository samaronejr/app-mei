"""Arm the dead-man's switch at migrate time, not at the scheduler's first tick.

Without this row a brand-new deployment has nothing to be stale, and the two states
"beat has never started" and "beat is healthy" would be indistinguishable to anything
that treated absence as acceptable. `scheduler_is_alive` already refuses to treat it
that way — a missing row reports dead — but then a cold boot would answer 503 for as
long as it took beat to reach its first tick, and the container would never come up.

Seeding here gives both properties at once: a cold boot starts with a fresh signal
inside the window, and a beat process that never starts ages out of it within fifteen
minutes and the probe says so.
"""

from django.apps import registry as app_registry
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

HEARTBEAT_NAME = "beat"


def arm(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Create the heartbeat row if it is not already there."""
    heartbeat = apps.get_model("obligations", "SchedulerHeartbeat")
    heartbeat.objects.get_or_create(name=HEARTBEAT_NAME)


def disarm(apps: app_registry.Apps, _editor: BaseDatabaseSchemaEditor | None) -> None:
    """Remove exactly the row this migration inserted."""
    heartbeat = apps.get_model("obligations", "SchedulerHeartbeat")
    heartbeat.objects.filter(name=HEARTBEAT_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [("obligations", "0006_obligation_and_heartbeat")]

    operations = [migrations.RunPython(arm, disarm)]
