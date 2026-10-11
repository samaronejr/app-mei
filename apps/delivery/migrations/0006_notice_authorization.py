"""No historical consent: nullable old bindings and guarded new notice inserts."""

import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import apps.core.identifiers
from apps.core.migrations._operations import DenyPortalAccess, EnableRLS

GUARDS = """
CREATE FUNCTION delivery_notice_policy_revision() RETURNS trigger
LANGUAGE plpgsql AS $policy$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'notice_policy_revision_retained' USING ERRCODE = '23514';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 THEN
            RAISE EXCEPTION 'notice_policy_initial_revision' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.id IS DISTINCT FROM OLD.id
          OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
          OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'notice_policy_revision_changed' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$policy$;
CREATE TRIGGER delivery_notice_policy_revision
BEFORE INSERT OR UPDATE OR DELETE ON delivery_tenantnoticepolicy
FOR EACH ROW EXECUTE FUNCTION delivery_notice_policy_revision();

CREATE FUNCTION delivery_notice_binding_guard() RETURNS trigger
LANGUAGE plpgsql AS $notice$
DECLARE
    binding jsonb;
    parent delivery_outboundmessage%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.notice_authorization_bytes, NEW.notice_authorization_digest)
           IS DISTINCT FROM
           ROW(OLD.notice_authorization_bytes, OLD.notice_authorization_digest)
           OR (OLD.notice_authorization_bytes IS NOT NULL AND
               ROW(NEW.id, NEW.tenant_id, NEW.client_id, NEW.kind, NEW.contact_id,
                   NEW.cycle_id, NEW.evidence_request_id, NEW.invite_id,
                   NEW.created_by_id, NEW.business_intent_key, NEW.retry_of_id,
                   NEW.content_digest)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.tenant_id, OLD.client_id, OLD.kind, OLD.contact_id,
                   OLD.cycle_id, OLD.evidence_request_id, OLD.invite_id,
                   OLD.created_by_id, OLD.business_intent_key, OLD.retry_of_id,
                   OLD.content_digest)) THEN
            RAISE EXCEPTION 'notice_authority_immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.kind IN ('approved_email', 'approved_publication_notice') THEN
        IF NEW.notice_authorization_bytes IS NOT NULL
           OR NEW.notice_authorization_digest IS NOT NULL THEN
            RAISE EXCEPTION 'consequential_notice_authority' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.notice_authorization_bytes IS NULL
       OR NEW.notice_authorization_digest IS NULL
       OR sha256(NEW.notice_authorization_bytes)
          IS DISTINCT FROM NEW.notice_authorization_digest THEN
        RAISE EXCEPTION 'notice_authority_required' USING ERRCODE = '23514';
    END IF;
    binding := convert_from(NEW.notice_authorization_bytes, 'UTF8')::jsonb;
    IF binding->>'schema' IS DISTINCT FROM 'axorim.notice-authorization.v1'
       OR binding->>'mode' IS DISTINCT FROM 'manual'
       OR binding->>'feature' IS DISTINCT FROM 'cycles_r1'
       OR binding->>'kind' IS DISTINCT FROM NEW.kind
       OR binding->>'tenant_id' IS DISTINCT FROM NEW.tenant_id::text
       OR binding->>'client_id' IS DISTINCT FROM NEW.client_id::text
       OR binding->>'contact_id' IS DISTINCT FROM NEW.contact_id::text
       OR binding->>'cycle_id' IS DISTINCT FROM NEW.cycle_id::text
       OR binding->>'request_id' IS DISTINCT FROM NEW.evidence_request_id::text
       OR binding->>'invite_id' IS DISTINCT FROM NEW.invite_id::text
       OR binding->>'issuer_id' IS DISTINCT FROM NEW.created_by_id::text
       OR binding->>'content_digest' IS DISTINCT FROM NEW.content_digest
       OR NEW.kind NOT IN ('notice', 'portal_invite', 'request_notice', 'request_reminder')
       OR coalesce(binding->'basis'->>'type', '') NOT IN ('playbook', 'tenant_policy') THEN
        RAISE EXCEPTION 'notice_authority_identity' USING ERRCODE = '23514';
    END IF;
    IF NEW.retry_of_id IS NULL THEN
        IF binding->>'root_intent' IS DISTINCT FROM NEW.business_intent_key THEN
            RAISE EXCEPTION 'notice_authority_root' USING ERRCODE = '23514';
        END IF;
    ELSE
        SELECT * INTO parent FROM delivery_outboundmessage
         WHERE id = NEW.retry_of_id AND tenant_id = NEW.tenant_id
           AND client_id = NEW.client_id;
        IF NOT FOUND
           OR NEW.notice_authorization_bytes IS DISTINCT FROM parent.notice_authorization_bytes
           OR NEW.notice_authorization_digest IS DISTINCT FROM parent.notice_authorization_digest
           OR NEW.kind IS DISTINCT FROM parent.kind
           OR NEW.content_digest IS DISTINCT FROM parent.content_digest
           OR NEW.created_by_id IS DISTINCT FROM parent.created_by_id
           OR NEW.contact_id IS DISTINCT FROM parent.contact_id
           OR NEW.cycle_id IS DISTINCT FROM parent.cycle_id
           OR NEW.evidence_request_id IS DISTINCT FROM parent.evidence_request_id
           OR NEW.invite_id IS DISTINCT FROM parent.invite_id THEN
            RAISE EXCEPTION 'notice_authority_retry_lineage' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$notice$;
CREATE TRIGGER delivery_notice_binding_guard
BEFORE INSERT OR UPDATE ON delivery_outboundmessage
FOR EACH ROW EXECUTE FUNCTION delivery_notice_binding_guard();
"""

REVERSE = """
DROP TRIGGER delivery_notice_binding_guard ON delivery_outboundmessage;
DROP FUNCTION delivery_notice_binding_guard();
DROP TRIGGER delivery_notice_policy_revision ON delivery_tenantnoticepolicy;
DROP FUNCTION delivery_notice_policy_revision();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("delivery", "0005_outbound_retry"),
        ("tenants", "0014_invite_originating_contact"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="outboundmessage",
            name="notice_authorization_bytes",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="notice_authorization_digest",
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="TenantNoticePolicy",
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
                ("version", models.PositiveIntegerField(default=1)),
                ("grants_json", models.JSONField(blank=True, default=list)),
                (
                    "acknowledged_version",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                ("ack_membership_id", models.UUIDField(blank=True, null=True)),
                (
                    "ack_membership_version",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("acknowledgement_code", models.CharField(blank=True, max_length=64)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "acknowledged_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        db_index=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="tenants.tenant",
                        verbose_name="tenant",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "default_manager_name": "objects",
                "base_manager_name": "all_objects",
                "indexes": [
                    models.Index(
                        fields=["tenant", "version"], name="notice_policy_revision_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant",), name="notice_policy_tenant_uniq"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(version__gt=0),
                        name="notice_policy_version_positive",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                acknowledged_version__isnull=True,
                                acknowledged_by__isnull=True,
                                ack_membership_id__isnull=True,
                                ack_membership_version__isnull=True,
                                acknowledged_at__isnull=True,
                                acknowledgement_code="",
                            )
                            | (
                                models.Q(
                                    acknowledged_version=models.F("version"),
                                    acknowledged_by__isnull=False,
                                    ack_membership_id__isnull=False,
                                    ack_membership_version__gt=0,
                                    acknowledged_at__isnull=False,
                                )
                                & ~models.Q(acknowledgement_code="")
                                & ~models.Q(grants_json=[])
                            )
                        ),
                        name="notice_policy_ack_complete",
                    ),
                ],
            },
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
        EnableRLS("TenantNoticePolicy"),
        DenyPortalAccess("TenantNoticePolicy"),
        migrations.RunSQL(GUARDS, REVERSE),
    ]
