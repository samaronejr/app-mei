"""An invitation may name a client, and its role must agree with whether it did.

W7's data contract. `Invite` was firm-side only: `invite_role_is_firm_side` admitted
`FIRM_ROLES` and nothing else, which is why the absence of this column was itself the
evidence that the gated portal invitation had not been pre-built. With the gate open the
invitation grows the same optional client the membership it creates already carries, and
the CHECK becomes the same two-armed rule as
`membership_role_matches_client_scope`: a firm role has no client, a client role names
one.

The constraint keeps its name. Renaming a CHECK is a drop-and-recreate that changes no
behaviour and would break two comments that cite it by name, and the arm the name points
at is still the one that matters most -- a client role on a client-less invitation is how
a portal seat would escape its client and become firm-wide at accept time.

`RemoveConstraint` runs BEFORE `AddField`, which is the order the autodetector chose and
the only one that works: the new condition names `client`, so adding it first would
declare a CHECK over a column that does not exist yet.

The composite foreign key closes the same hole `membership_tenant_client_fk` closes on
the sibling table (`0002_portal_client_membership.py:109-120`), for the same reason.
PostgreSQL evaluates referential integrity with row-level security BYPASSED, and
`tenants_invite` carries no policy at all, so Django's single-column `client_id` key
proves only that some firm somewhere owns that client. Pairing `tenant_id` into the key
makes an invitation pointing at another firm's client unrepresentable rather than merely
unredeemable -- without it such a row can be written, and only blows up as an
IntegrityError in the invitee's face when they present a token that was always doomed.
MATCH SIMPLE is the default, so the NULL `client_id` on every firm-side invitation
satisfies the constraint without being checked.
"""

import django.db.models.deletion
from django.db import migrations, models

from apps.core.migrations._composite_fk import add_tenant_composite_fk


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0011_alter_clientassignment_id_alter_clientcompany_id_and_more"),
        ("tenants", "0005_invite_revoked_at"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="invite",
            name="invite_role_is_firm_side",
        ),
        migrations.AddField(
            model_name="invite",
            name="client",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="portal_invites",
                to="clients.clientcompany",
                verbose_name="client",
            ),
        ),
        migrations.AddConstraint(
            model_name="invite",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("client__isnull", True),
                        (
                            "role__in",
                            frozenset(
                                ["operations_admin", "owner", "staff_accountant"]
                            ),
                        ),
                    ),
                    models.Q(
                        ("client__isnull", False),
                        (
                            "role__in",
                            frozenset(["client_collaborator", "client_owner"]),
                        ),
                    ),
                    _connector="OR",
                ),
                name="invite_role_is_firm_side",
            ),
        ),
        add_tenant_composite_fk(
            table="tenants_invite",
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="invite_tenant_client_fk",
        ),
    ]
