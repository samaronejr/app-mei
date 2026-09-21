# ISOLATION PRIMITIVES AND STARTUP CONTRACTS

## OVERVIEW
Shared tenancy machinery, model bases, and system checks; score 12, cross-domain infrastructure boundary.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Ambient tenant/client scope | `tenancy.py` | ContextVars and PostgreSQL GUCs |
| Automatic queryset filtering | `managers.py` | Tenant/platform/root managers |
| Model foundations | `models.py` | Scoped models and UUIDv7 primary keys |
| Isolation schema operations | `rls.py`, `migrations/` | RLS and composite relationships |
| Security boot invariants | `checks.py` | Middleware, privileges, portal gates |
| Worker execution scope | `tasks.py` | `TenantTask`, `PlatformTask` |
| Command execution scope | `management/` | Tenant-aware base and `seed_demo` |
| Health endpoint | `views.py` | `healthz` |
| Test-only model | `tests/` | Installed fixture app, not the main pytest suite |

## CONVENTIONS
- `tenant_context` updates both the ContextVar and transaction-local `app.tenant_id`.
- `client_context` handles only the client dimension; establish tenant scope separately.
- Missing scope raises instead of allowing a silent zero-row background sweep.
- Successful nested atomic blocks explicitly restore the previous GUC value.
- PostgreSQL merges SET LOCAL upward on savepoint release; savepoints do not reset it.
- On exceptions, rollback restores the setting; avoid SQL on the aborted transaction.
- ContextVar cleanup always resets its token so reused workers retain no ambient identity.
- `client_context` alone is not a database confinement boundary.
- Restrictive client policies apply only after assuming `app_portal`.
- Management commands and Celery use these primitives outside the request lifecycle.

## ANTI-PATTERNS
- Do not replace paired context setup with a bare ContextVar `set()`.
- Do not put GUC restoration in a finally block that can run on an aborted transaction.
- Do not infer tenant isolation solely from a scoped manager's queryset.
- Do not use UUIDv7 ordering as business chronology; order by the business field.
- Do not remove custom system checks merely because pytest is green.

## RELATED CHECKS
- `uv run pytest tests/core tests/isolation`
- `uv run python manage.py check --settings=config.settings.test`
- CI also runs checks with development and production settings.
