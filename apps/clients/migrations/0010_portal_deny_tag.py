"""Shut `app_portal` out of the firm's tag vocabulary.

`clients_tag` is firm-wide rather than client-scoped: leaking it would expose how an
accounting firm classifies its whole client base to any one of those clients. It has
row-level security enabled, so this RESTRICTIVE deny policy is evaluated.

Note this is the tag *vocabulary*, not `clients_clienttag` — that join table carries a
`client_id` and is confined by `EnablePortalClientRLS` in 0008 instead.
"""

from django.db import migrations

from apps.core.migrations._operations import DenyPortalAccess


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0009_portal_clientcompany_policy"),
    ]

    operations = [
        DenyPortalAccess("Tag"),
    ]
