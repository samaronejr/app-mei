import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import apps.core.identifiers
from apps.core.migrations._composite_fk import add_tenant_composite_fk
from apps.core.migrations._operations import (
    DenyPortalAccess,
    EnablePortalClientRLS,
    EnableRLS,
)

FROZEN = """
CREATE FUNCTION cycles_guard_bulkoperation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.id, NEW.tenant_id, NEW.kind, NEW.requested_by_id,
           NEW.snapshot_json, NEW.snapshot_digest, NEW.valid_until,
           NEW.idempotency_key, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.kind, OLD.requested_by_id,
           OLD.snapshot_json, OLD.snapshot_digest, OLD.valid_until,
           OLD.idempotency_key, OLD.created_at)
    THEN
        RAISE EXCEPTION 'bulk preview identity is immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.version < OLD.version OR
       (OLD.state IN ('completed', 'completed_with_failures', 'cancelled')
        AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION 'bulk terminal state is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER cycles_bulkoperation_frozen
BEFORE UPDATE ON cycles_bulkoperation FOR EACH ROW
EXECUTE FUNCTION cycles_guard_bulkoperation();

CREATE FUNCTION cycles_guard_bulkitem() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bulk receipts cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF ROW(NEW.id, NEW.tenant_id, NEW.client_id, NEW.operation_id,
           NEW.subject_ref, NEW.expected_version, NEW.effect_key, NEW.position)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tenant_id, OLD.client_id, OLD.operation_id,
           OLD.subject_ref, OLD.expected_version, OLD.effect_key, OLD.position)
       OR NEW.fence < OLD.fence
    THEN
        RAISE EXCEPTION 'bulk item identity and fence are immutable'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.state NOT IN ('pending', 'claimed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'bulk terminal receipt is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER cycles_bulkitem_frozen
BEFORE UPDATE OR DELETE ON cycles_bulkitem FOR EACH ROW
EXECUTE FUNCTION cycles_guard_bulkitem();
"""
UNFREEZE = """
DROP TRIGGER cycles_bulkitem_frozen ON cycles_bulkitem;
DROP FUNCTION cycles_guard_bulkitem();
DROP TRIGGER cycles_bulkoperation_frozen ON cycles_bulkoperation;
DROP FUNCTION cycles_guard_bulkoperation();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("clients", "0015_import_batches_and_versions"),
        ("cycles", "0008_exact_effect_approvals"),
        ("tenants", "0010_approval_authority"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BulkOperation",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("open_cycles", "Abrir ciclos"),
                            ("send_request_notices", "Enviar avisos de solicitações"),
                            ("migrate_open_cycles_to_version", "Migrar ciclos abertos"),
                            ("approve_items", "Aprovar efeitos por item"),
                        ],
                        max_length=32,
                    ),
                ),
                ("snapshot_json", models.JSONField()),
                ("snapshot_digest", models.CharField(max_length=64)),
                ("valid_until", models.DateTimeField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("preview", "Prévia"),
                            ("queued", "Na fila"),
                            ("running", "Em andamento"),
                            ("cancelling", "Cancelando"),
                            ("cancelled", "Cancelado"),
                            ("completed", "Concluído"),
                            ("completed_with_failures", "Concluído com falhas"),
                        ],
                        default="preview",
                        max_length=24,
                    ),
                ),
                ("idempotency_key", models.CharField(max_length=64)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("confirmed_at", models.DateTimeField(null=True)),
                ("finished_at", models.DateTimeField(null=True)),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="bulk_operations",
                        to=settings.AUTH_USER_MODEL,
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
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name="BulkItem",
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
                ("subject_ref", models.CharField(max_length=64)),
                ("expected_version", models.PositiveIntegerField()),
                ("effect_key", models.CharField(max_length=64)),
                ("position", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pendente"),
                            ("claimed", "Em processamento"),
                            ("done", "Processado"),
                            ("skipped_changed", "Ignorado: registro alterado"),
                            ("skipped_forbidden", "Ignorado: acesso revogado"),
                            ("skipped_ineligible", "Não elegível"),
                            ("skipped_excluded", "Excluído da execução"),
                            ("cancelled", "Cancelado sem executar"),
                            ("failed", "Falhou"),
                        ],
                        default="pending",
                        max_length=24,
                    ),
                ),
                ("result_json", models.JSONField(default=dict)),
                ("fence", models.PositiveIntegerField(default=0)),
                ("lease_owner", models.UUIDField(null=True)),
                ("lease_until", models.DateTimeField(null=True)),
                ("finished_at", models.DateTimeField(null=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="clients.clientcompany",
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
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="items",
                        to="cycles.bulkoperation",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddIndex(
            model_name="bulkoperation",
            index=models.Index(
                fields=["tenant", "state", "created_at"], name="bulkoperation_queue_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkoperation",
            constraint=models.UniqueConstraint(
                fields=("tenant", "id"), name="bulkoperation_scope_id_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkoperation",
            constraint=models.UniqueConstraint(
                fields=("tenant", "idempotency_key"), name="bulkoperation_intent_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    state__in=[
                        "preview",
                        "queued",
                        "running",
                        "cancelling",
                        "cancelled",
                        "completed",
                        "completed_with_failures",
                    ]
                ),
                name="bulkoperation_known_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(version__gt=0),
                name="bulkoperation_positive_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(valid_until__gt=models.F("created_at")),
                name="bulkoperation_preview_interval",
            ),
        ),
        migrations.AddIndex(
            model_name="bulkitem",
            index=models.Index(fields=["tenant", "client"], name="bulkitem_scope_idx"),
        ),
        migrations.AddIndex(
            model_name="bulkitem",
            index=models.Index(
                fields=["tenant", "operation", "state"], name="bulkitem_resume_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkitem",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "id"), name="bulkitem_scope_id_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkitem",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "operation", "subject_ref"),
                name="bulkitem_subject_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkitem",
            constraint=models.UniqueConstraint(
                fields=("tenant", "client", "effect_key"), name="bulkitem_effect_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    state__in=[
                        "pending",
                        "claimed",
                        "done",
                        "skipped_changed",
                        "skipped_forbidden",
                        "skipped_ineligible",
                        "skipped_excluded",
                        "cancelled",
                        "failed",
                    ]
                ),
                name="bulkitem_known_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkitem",
            constraint=models.CheckConstraint(
                condition=models.Q(expected_version__gt=0),
                name="bulkitem_positive_expectation",
            ),
        ),
        migrations.AddConstraint(
            model_name="bulkitem",
            constraint=models.CheckConstraint(
                condition=~models.Q(state="claimed")
                | models.Q(
                    fence__gt=0, lease_owner__isnull=False, lease_until__isnull=False
                ),
                name="bulkitem_claim_has_fence",
            ),
        ),
        EnableRLS("BulkOperation"),
        EnableRLS("BulkItem"),
        DenyPortalAccess("BulkOperation"),
        EnablePortalClientRLS("BulkItem"),
        DenyPortalAccess("BulkItem"),
        add_tenant_composite_fk(
            table="cycles_bulkitem",
            column="client_id",
            parent_table="clients_clientcompany",
            constraint="bulkitem_client_pair_fk",
        ),
        add_tenant_composite_fk(
            table="cycles_bulkitem",
            column="operation_id",
            parent_table="cycles_bulkoperation",
            constraint="bulkitem_operation_pair_fk",
        ),
        migrations.RunSQL(FROZEN, UNFREEZE),
        # Dependent replays run after the test fixture's initial blanket grant.
        # Restore this table contract explicitly, as the T15 migration does.
        migrations.RunSQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON cycles_bulkoperation, "
            "cycles_bulkitem TO app_runtime;",
            "REVOKE ALL ON cycles_bulkoperation, cycles_bulkitem FROM app_runtime;",
        ),
    ]
