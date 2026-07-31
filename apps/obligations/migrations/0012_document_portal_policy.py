"""Confine `app_portal` to a single client on the document table.

Lands with the migration that creates the table rather than in a later one, and not by
choice: `test_every_tenant_table_has_a_deliberate_portal_decision` fails the build the
moment a table carrying `tenant_id` exists without an explicit portal decision. A tenant
table with no decision is precisely what that meta-test is for, so the two cannot be
separate commits without leaving the tree red in between.

This is also the assertion the C2 retirement rests on. That argument -- that every object
a portal request can fetch is already client-matched, so an object-dimension check can
never refuse -- was verified against the six tables that existed then. This is the
seventh, and W9 is what keeps the argument true rather than merely true so far.
"""

from django.db import migrations

from apps.core.migrations._operations import EnablePortalClientRLS


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0011_document"),
    ]

    operations = [
        EnablePortalClientRLS("Document"),
    ]
