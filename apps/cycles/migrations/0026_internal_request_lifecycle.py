"""Keep legacy and new firm-only requests private throughout their lifecycle."""

from django.db import migrations

from apps.cycles.migrations._request_cutover import install, refuse_exposure


class Migration(migrations.Migration):
    dependencies = [("cycles", "0025_portal_internal_request_boundary")]

    operations = [
        # Also covers an installation that stopped at the original 0025.
        # A corrected 0025 already repaired and installed the guards atomically.
        migrations.RunPython(install, migrations.RunPython.noop),
        migrations.RunPython(migrations.RunPython.noop, refuse_exposure),
    ]
