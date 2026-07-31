"""Pair the document's obligation reference on `client_id`, not only on `tenant_id`.

C3, closed one dimension down. A tenant-paired foreign key proves the obligation belongs
to the same firm and says nothing about which client inside it, so a portal user could
attach their own document to another client's obligation: the `WITH CHECK` policy
inspects the child's own `client_id`, which is honest, and referential checks bypass row
security by design, so the reference itself was unconstrained.

Django's own single-column `obligation_id` foreign key is KEPT alongside rather than
dropped. The composite subsumes it, but Django manages that one from the model state, so
dropping it here would make the database diverge from what `makemigrations` believes and
the next unrelated migration would try to recreate it. The same choice was made for
`obligation_tenant_client_fk` on `obligations_obligation`.

The parent gains `UNIQUE (tenant_id, client_id, id)` because a composite foreign key must
reference exactly a unique constraint. `id` alone is already the primary key, so this adds
no integrity the table lacked -- it exists so the constraint below is expressible.

Not paired here, and deliberately: `tenant_id -> tenants_tenant` and `uploaded_by_id ->
accounts_user` have no `client_id` on their parents, and `client_id ->
clients_clientcompany` is the client registry itself, whose pk IS the client identity, so
its correct form is the tenant-paired one it already carries. Those three are W7's
exemption list.
"""

from django.db import migrations

from apps.core.migrations._composite_fk import (
    add_client_composite_fk,
    add_client_parent_unique,
)


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0012_document_portal_policy"),
    ]

    operations = [
        add_client_parent_unique(
            table="obligations_obligation",
            constraint="obligation_tenant_client_id_uniq",
        ),
        add_client_composite_fk(
            table="obligations_document",
            column="obligation_id",
            parent_table="obligations_obligation",
            constraint="document_obligation_tenant_client_fk",
        ),
    ]
