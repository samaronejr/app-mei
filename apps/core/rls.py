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
        "audit_accesslog",
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
