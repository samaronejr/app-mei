"""Shut `app_portal` out of the tenant audit trail.

`audit_event` has row-level security enabled, so a RESTRICTIVE deny policy on it is
actually evaluated. In Phase 2a the portal is already held off this table by the SELECT
allow-list in `ops/sql/roles.sql`, which raises `permission denied` before row-level
security is ever consulted; this policy is the backstop for Phase 2b, when the grant
widens for document upload.
"""

from django.db import migrations

from apps.core.migrations._operations import DenyPortalAccess


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0003_datasubjectrequest"),
    ]

    operations = [
        DenyPortalAccess("Event"),
    ]
