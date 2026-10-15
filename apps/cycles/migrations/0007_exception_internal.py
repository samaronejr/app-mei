"""Separate private exception content without changing aggregate identity.

The migrator copies every tenant's actual stored values, not current registry
defaults. Both directions deny portal rows while private columns exist on the
public table. Reversal stays closed even when post_migrate repairs SELECT.
"""

import django.db.models.deletion
import django.db.models.manager
from django.db import migrations, models

import apps.core.identifiers
from apps.core.migrations._composite_fk import (
    add_client_composite_fk,
    add_tenant_composite_fk,
)
from apps.core.migrations._operations import EnablePortalClientRLS, EnableRLS

POLICY = "cycles_exception_portal_client_isolation"
CLIENT = "NULLIF(current_setting('app.client_id', true), '')::uuid"
DENIED = f"client_id = {CLIENT} AND false"
VISIBLE = f"client_id = CASE WHEN client_visible THEN {CLIENT} END"


def policy(predicate: str) -> str:
    return (
        f"DROP POLICY {POLICY} ON cycles_exception; "
        f"CREATE POLICY {POLICY} ON cycles_exception AS RESTRICTIVE FOR ALL "
        f"TO app_portal USING ({predicate}) WITH CHECK ({predicate});"
    )


class Migration(migrations.Migration):
    dependencies = [("cycles", "0006_exception_client_visibility")]

    operations = [
        migrations.RunSQL(policy(DENIED), policy(DENIED)),
        # RemoveField reverses by recreating this column on populated rows.
        # Supply its temporary fill value before copying the original text back.
        migrations.AlterField(
            model_name="exception",
            name="next_action",
            field=models.CharField(default="", max_length=255),
        ),
        migrations.CreateModel(
            name="ExceptionInternal",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=apps.core.identifiers.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("next_action", models.CharField(max_length=255)),
                ("evidence_refs", models.JSONField(default=list)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="clients.clientcompany",
                    ),
                ),
                (
                    "exception",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="internals",
                        to="cycles.exception",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="tenants.tenant",
                        verbose_name="tenant",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "indexes": [
                    models.Index(
                        fields=["tenant", "client"], name="exceptioninternal_scope_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "client", "id"),
                        name="exceptioninternal_scope_id_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("tenant", "client", "exception"),
                        name="exceptioninternal_exception_uniq",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("ExceptionInternal"),
        EnablePortalClientRLS("ExceptionInternal"),
        # A populated reversal/reapply can recreate the table under app_test,
        # whose default privileges in the test DB differ from app_migrator's.
        # Keep the normal runtime contract without granting anything to the portal.
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON cycles_exceptioninternal TO app_runtime;",
            "REVOKE SELECT, INSERT, UPDATE, DELETE "
            "ON cycles_exceptioninternal FROM app_runtime;",
        ),
        add_tenant_composite_fk(
            table="cycles_exceptioninternal",
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="exceptioninternal_client_pair_fk",
        ),
        add_client_composite_fk(
            table="cycles_exceptioninternal",
            column="exception_id",
            parent_table="cycles_exception",
            constraint="exceptioninternal_exception_pair_fk",
        ),
        migrations.RunSQL(
            """
            INSERT INTO cycles_exceptioninternal
                (id, tenant_id, client_id, exception_id, next_action, evidence_refs)
            SELECT gen_random_uuid(), tenant_id, client_id, id, next_action, evidence_refs
            FROM cycles_exception;
            """,
            """
            UPDATE cycles_exception AS problem
            SET next_action = private.next_action, evidence_refs = private.evidence_refs
            FROM cycles_exceptioninternal AS private
            WHERE private.exception_id = problem.id
                AND private.tenant_id = problem.tenant_id
                AND private.client_id = problem.client_id;
            """,
        ),
        migrations.RemoveField(model_name="exception", name="next_action"),
        migrations.RemoveField(model_name="exception", name="evidence_refs"),
        migrations.RunSQL(policy(VISIBLE), policy(DENIED)),
    ]
