"""Reconcile every concrete registered model against explicit isolation decisions."""

from typing import Final

from django.apps import apps as django_apps

from tests.isolation.axorim_seed import axorim_models

# Independent of axorim_models(): its whole-app enumeration must not silently
# accept a future model with no reviewed inventory entry or seed decision.
SWEEP_LABELS: Final = frozenset(
    {
        "authz.UserCapabilityGrant",
        "automation.AgentRun",
        "automation.EvalRun",
        "clients.ClientContact",
        "clients.Engagement",
        "clients.ImportBatch",
        "clients.ImportRow",
        "clients.TaxProfile",
        "cycles.ActionExecution",
        "cycles.ActionProposal",
        "cycles.Approval",
        "cycles.BriefingFeedback",
        "cycles.BulkItem",
        "cycles.BulkOperation",
        "cycles.ClientAcknowledgement",
        "cycles.ClientPublication",
        "cycles.CompletionGeneration",
        "cycles.CompletionJustification",
        "cycles.ContentPurgeClaim",
        "cycles.CycleAmendment",
        "cycles.CycleEvent",
        "cycles.CycleTask",
        "cycles.DocumentIntake",
        "cycles.EvidenceExport",
        "cycles.EvidenceIndex",
        "cycles.EvidenceLink",
        "cycles.EvidenceRequest",
        "cycles.Exception",
        "cycles.ExceptionInternal",
        "cycles.ExportAttempt",
        "cycles.ExtractedField",
        "cycles.InternalNote",
        "cycles.LegalHold",
        "cycles.Playbook",
        "cycles.PlaybookStep",
        "cycles.PlaybookVersion",
        "cycles.RequestInternal",
        "cycles.Review",
        "cycles.SavedView",
        "cycles.ServiceCycle",
        "delivery.AutomationPause",
        "delivery.DeliveryAttempt",
        "delivery.DeliveryEffect",
        "delivery.OutboundMessage",
        "delivery.TenantNoticePolicy",
        "integrations.ClientProviderLink",
        "integrations.ProviderConnection",
        "obligations.SourceObservation",
        "tenants.AccountExport",
        "tenants.AccountExportAttempt",
        "tenants.BillingAccount",
        "tenants.FirmOutboundMessage",
        "tenants.NotificationPreference",
        "tenants.SupportGrant",
        "tenants.UsageRecord",
    }
)

# Exact labels, never app/table globs. Legacy tenant models are exempt ONLY from
# the R1 generic seed sweep, not from the existing RLS/portal coverage suites.
# The complete per-model evidence table lives in .evidence/AXR1-038-prep.md.
SWEEP_EXEMPTIONS: Final[dict[str, str]] = {
    "accounts.User": "Global login identity, resolved before tenant context.",
    "accounts.User_groups": "Global identity/auth group through table.",
    "accounts.User_user_permissions": "Global identity/auth permission through table.",
    "account.EmailAddress": "Global allauth identity verification, before tenancy.",
    "account.EmailConfirmation": "Global allauth identity verification token.",
    "admin.LogEntry": "Django admin infrastructure, not an R1 tenant record.",
    "auth.Group": "Global Django permission infrastructure.",
    "auth.Group_permissions": "Global Django permission through table.",
    "auth.Permission": "Global Django permission infrastructure.",
    "contenttypes.ContentType": "Django model metadata, shared across firms.",
    "sessions.Session": "Login sessions exist before tenant resolution.",
    "mfa.Authenticator": "Global identity second factor, checked before tenancy.",
    "django_celery_beat.ClockedSchedule": "Global worker scheduling infrastructure.",
    "django_celery_beat.CrontabSchedule": "Global worker scheduling infrastructure.",
    "django_celery_beat.IntervalSchedule": "Global worker scheduling infrastructure.",
    "django_celery_beat.PeriodicTask": "Global worker scheduling infrastructure.",
    "django_celery_beat.PeriodicTasks": "Global scheduler change counter.",
    "django_celery_beat.SolarSchedule": "Global worker scheduling infrastructure.",
    "authz.Capability": "Platform capability matrix; identical for every firm.",
    "authz.RoleGrant": "Platform role matrix; not per-user tenant authority.",
    "tenants.Tenant": "Tenancy root, resolved before app.tenant_id can exist.",
    "tenants.Membership": "Bootstrap roster discovers tenancy; app-layer scoped.",
    "tenants.Invite": "Pre-auth bearer-token bootstrap; app-layer scoped.",
    "audit.AccessLog": "Nullable tenant permits pre-auth Marco Civil request logging.",
    "audit.PlatformEvent": "Pre-auth/cross-tenant identity audit; global by design.",
    "audit.DataSubjectRequest": "Unauthenticated LGPD intake before tenant resolution.",
    "clients.OnboardingItemTemplate": "Shared national onboarding reference checklist.",
    "fiscal.Municipality": "Shared municipality reference data.",
    "fiscal.MunicipalityCapability": "Shared municipal fiscal capability reference.",
    "fiscal.MunicipalitySeedRow": "Registry seed ownership ledger; no tenant data.",
    "authz.CapabilitySeedRow": "Matrix seed ownership ledger; no tenant data.",
    "obligations.FiscalParameter": "Shared national fiscal reference data.",
    "obligations.Holiday": "Shared statutory holiday reference data.",
    "obligations.ObligationType": "Shared statutory obligation reference data.",
    "obligations.ObligationDueRule": "Shared statutory deadline reference data.",
    "obligations.SchedulerHeartbeat": "Global worker health, no client/tenant payload.",
    "core_tests.ExampleTenantModel": (
        "Test-only fixture; dedicated real-role tests in "
        "tests/isolation/test_cross_tenant.py."
    ),
    "audit.Event": (
        "Pre-R1 append-only tenant audit; tests/audit/test_append_only.py and "
        "tests/isolation/test_deny_portal_backstop.py; structural RLS remains required."
    ),
    "clients.ClientCompany": (
        "Pre-R1 client identity; tests/isolation/test_same_cnpj_two_firms.py and "
        "test_portal_client_isolation.py; structural RLS remains required."
    ),
    "clients.ClientAssignment": (
        "Pre-R1 roster link; tests/clients/test_assignments.py and "
        "tests/isolation/test_portal_client_isolation.py."
    ),
    "clients.ClientTag": (
        "Pre-R1 tag link; tests/clients/test_assignments.py and "
        "tests/isolation/test_portal_client_isolation.py."
    ),
    "clients.OnboardingItem": (
        "Pre-R1 checklist; tests/isolation/test_portal_client_isolation.py "
        "has populated owning/sibling controls; structural RLS remains required."
    ),
    "clients.Tag": (
        "Pre-R1 firm tag dictionary; tests/isolation/test_deny_portal_backstop.py "
        "includes a transactional dropped-policy witness."
    ),
    "obligations.Document": (
        "Pre-R1 vault metadata; tests/isolation/test_document_client_paired_fk.py "
        "and test_portal_write_refusal.py exercise real portal writes and composites."
    ),
    "obligations.MonthlyRevenue": (
        "Pre-R1 revenue; tests/isolation/test_portal_client_isolation.py "
        "has populated owning/sibling controls; structural RLS remains required."
    ),
    "obligations.Obligation": (
        "Pre-R1 obligation; tests/isolation/test_portal_client_isolation.py and "
        "test_portal_write_refusal.py; structural RLS remains required."
    ),
}


def test_every_concrete_model_has_behavioral_coverage_or_justified_exemption() -> None:
    # Given every concrete model, including implicit M2M through tables. Django
    # excludes swapped-out auth.User; proxies/abstract bases have no own table.
    registered = {
        model._meta.label
        for model in django_apps.get_models(include_auto_created=True)
        if not model._meta.abstract and not model._meta.proxy
    }
    swept = {model._meta.label for model in axorim_models()}
    exempt = set(SWEEP_EXEMPTIONS)
    assert registered
    assert all(reason.strip() for reason in SWEEP_EXEMPTIONS.values())
    assert swept.isdisjoint(exempt), sorted(swept & exempt)

    # When registry and behavior inventory are reconciled independently.
    missing = sorted(registered - swept - exempt)
    stale = sorted((swept | exempt) - registered)

    # Then omissions are named (including the seven at the db8b4a2 baseline).
    assert not missing, (
        f"models without behavioral sweep or justified exemption: {missing}"
    )
    assert not stale, f"inventory entries without concrete registered models: {stale}"
    assert swept == SWEEP_LABELS, (
        "behavioral sweep differs from reviewed inventory: "
        f"unreviewed={sorted(swept - SWEEP_LABELS)}, "
        f"unswept={sorted(SWEEP_LABELS - swept)}"
    )
