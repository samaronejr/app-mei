"""Which tables are allowed to carry no tenant-isolation policy, and why.

The coverage meta-test enumerates every table in the database and requires each one to
be either tenant-scoped-with-a-policy or listed here. A new business table that forgets
its `EnableRLS` operation therefore turns the build red instead of shipping a silent
cross-tenant read.

Entries may be glob patterns, so membership must be tested with `fnmatch.fnmatchcase`
and never with `in` — `"auth_user" in NON_TENANT_TABLES` is `False`.
"""

from fnmatch import fnmatchcase

NON_TENANT_TABLES: frozenset[str] = frozenset(
    {
        # Tenancy roots. The middleware reads these to *discover* the tenant, before
        # app.tenant_id can exist, so an app.tenant_id policy here would return zero
        # rows under the fail-closed predicate and nobody could ever sign in.
        "tenants_tenant",
        "tenants_membership",
        "tenants_invite",
        # Django, auth, and scheduling infrastructure.
        "auth_*",
        "django_*",
        "django_celery_beat_*",
        # The custom user model. It matches none of the globs above: "account_*"
        # requires that literal prefix and "accounts_user" does not match it.
        "accounts_*",
        # allauth, which is covered by neither auth_* nor django_*.
        "account_*",
        "mfa_*",
        "socialaccount_*",
        "usersessions_*",
        # Platform-level reference and control data, shared across every tenant.
        "authz_capability",
        "authz_rolegrant",
        # The standard onboarding checklist. What a MEI must have in order is a
        # property of Brazilian tax practice, not of any one firm, so a
        # per-tenant copy would let one firm's list drift from another's. The
        # per-client rows it generates are tenant-scoped and policed.
        "clients_onboardingitemtemplate",
        "audit_accesslog",
        # Identity events that happen BEFORE a tenant is resolved. A policy here
        # would reject the login-failure record an investigation needs most.
        "audit_platformevent",
        # A rights request arrives unauthenticated, before any tenant exists. A
        # fail-closed policy would turn a statutory intake channel into a 500.
        "audit_datasubjectrequest",
        # Which NFS-e system a municipality runs is a fact about that municipality,
        # not about any one accounting firm. A per-tenant copy would let two firms
        # hold contradictory answers about the same city and would make every firm
        # rediscover the same municipal variance independently. Created in T-035.
        "fiscal_municipality",
        "fiscal_municipalitycapability",
        "obligations_fiscalparameter",
        "obligations_obligationtype",
        "obligations_holiday",
        "obligations_schedulerheartbeat",
    },
)


def is_exempt_from_tenant_policy(table: str) -> bool:
    """Report whether a table is allow-listed to carry no tenant-isolation policy."""
    return any(fnmatchcase(table, pattern) for pattern in NON_TENANT_TABLES)


# Tables the portal coverage meta-test must not demand a portal policy for, each with
# the reason. A RESTRICTIVE policy is only evaluated when row-level security is enabled
# on the table; writing one onto a table where it is not is stored and never evaluated,
# which reads as "covered" while protecting nothing. Every table below has RLS
# deliberately disabled, so the portal is held off it by the SELECT allow-list in
# ops/sql/roles.sql instead — asserted in both directions by the meta-test.
PORTAL_DECISION_EXEMPT: dict[str, str] = {
    "audit_accesslog": (
        "RLS disabled by Phase-1 design: tenant is nullable so pre-authentication and "
        "anonymous requests are still recorded for Marco Civil. Written in the "
        "response phase as app_runtime, outside the portal transaction. app_portal "
        "holds no SELECT privilege on it."
    ),
    "audit_platformevent": (
        "Platform-level and cross-tenant by design, in NON_TENANT_TABLES. Buffered and "
        "flushed outside the request transaction. app_portal holds no SELECT privilege."
    ),
    "audit_datasubjectrequest": (
        "LGPD intake is a platform flow in Phase 2a, in NON_TENANT_TABLES. "
        "app_portal holds no SELECT privilege."
    ),
    "tenants_membership": (
        "Tenancy root, deliberately unpoliced: the middleware reads it to discover "
        "the tenant before any GUC exists, as app_runtime and before SET LOCAL ROLE. "
        "Gains a nullable client_id in T-059 and stays exempt, because exemption is "
        "evaluated before the client-column branch. app_portal holds no SELECT "
        "privilege."
    ),
    "tenants_invite": (
        "Tenancy root, same bootstrap reason as tenants_membership. "
        "app_portal holds no SELECT privilege."
    ),
    "core_tests_exampletenantmodel": (
        "Test-only fixture model, installed by config.settings.test alone. Carries no "
        "client_id, and a deny policy on it would be dead weight."
    ),
}


def portal_decision_exemption(table: str) -> str | None:
    """Return why a table needs no portal policy, or None if it needs one."""
    for pattern, justification in PORTAL_DECISION_EXEMPT.items():
        if fnmatchcase(table, pattern):
            return justification
    return None
