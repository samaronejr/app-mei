"""Permit only the implemented system reminder envelope; never broaden old consent."""

from importlib import import_module

from django.db import migrations, models

# Historical migration constants, not runtime policy. The root-corrected T48 body
# parenthesizes CASE inside IF; both that fix and every T53 identity/refusal remain.
BASE_GUARD = import_module(
    "apps.delivery.migrations.0007_contact_notice_origin"
).FORWARD
FORWARD = BASE_GUARD.replace(
    "OR binding->>'mode' IS DISTINCT FROM 'manual'",
    """OR coalesce(binding->>'mode', '') NOT IN ('manual', 'automatic')
       OR (binding->>'mode' = 'automatic' AND (
           NEW.kind IS DISTINCT FROM 'request_reminder'
           OR NEW.cycle_id IS NULL
           OR NEW.evidence_request_id IS NULL
           OR NEW.proposal_id IS NOT NULL
           OR binding->>'system_operation' IS DISTINCT FROM 'sweep_reminders'
           OR binding->'rule_version' IS DISTINCT FROM '1'::jsonb
           OR jsonb_typeof(binding->'occurrence') IS DISTINCT FROM 'number'
           OR jsonb_typeof(binding->'due_on') IS DISTINCT FROM 'string'
           OR jsonb_typeof(binding->'expires_at') IS DISTINCT FROM 'string'
           OR binding->'basis'->>'type' IS DISTINCT FROM 'tenant_policy'
           OR binding->>'issuer_id' IS DISTINCT FROM binding->'basis'->>'owner_id'
           OR binding->>'issuer_membership_id'
              IS DISTINCT FROM binding->'basis'->>'membership_id'
           OR binding->'issuer_membership_version'
              IS DISTINCT FROM binding->'basis'->'membership_version'
       ))""",
)


class Migration(migrations.Migration):
    dependencies = [("delivery", "0007_contact_notice_origin")]

    operations = [
        migrations.AddField(
            model_name="automationpause",
            name="reminder_cursor",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="automationpause",
            name="reminder_contact_cursor",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.RunSQL(FORWARD, BASE_GUARD),
    ]
