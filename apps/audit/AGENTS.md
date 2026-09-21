# AUDIT STREAMS

## OVERVIEW
Tenant, platform, access, and privacy-request records; score 9, distinct transaction-sensitive domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Event schema/actions | `models.py` | Separate streams with different mutability |
| Write an event | `services.py` | `record_event`, `record_platform_event` |
| Failed-request identity audit | `middleware.py` | Buffer flush outside tenant transaction |
| Login/logout/MFA hooks | `signals.py` | Identity event producers |
| Scoped admin screens | `admin.py` | `TenantScopedAdminMixin` |
| Access retention | `tasks.py` | `purge_access_logs` |
| Database append-only enforcement | `migrations/` | Triggers and privilege revocation |

## CONVENTIONS
- `Event` is tenant-attributable; no tenant raises instead of falling back to platform.
- `record_event` can enter an explicit tenant context when the ambient one differs.
- `PlatformEvent` handles identity events before tenant resolution or without a tenant.
- Request platform events buffer in a ContextVar; middleware flushes after tenant rollback/commit.
- Outside a request buffer, `record_platform_event` writes immediately.
- Buffer cleanup resets the ContextVar token, including exceptional paths.
- `ObjectRef` stores type/id instead of copying the referenced person's data.
- `Origin` carries identity-event actor, subject, and source IP separately.
- Access logs have retention semantics, unlike append-only audit event history.
- Data-subject requests are mutable intake records, not immutable audit events.

## ANTI-PATTERNS
- Do not move platform-event flushing into the failing view's transaction.
- Do not put names, CPF/CNPJ, or copied personal records into event metadata.
- Do not purge or edit the append-only tenant/platform event streams.
- Do not restore UPDATE, DELETE, or TRUNCATE through broad runtime grants.
- Do not use the tenant event stream for an event that precedes tenant identification.

## RELATED CHECKS
- `uv run pytest tests/audit`
- Access-log tests cover credential-path redaction and rollback behavior.
- `tests/conftest.py` must reapply append-only revokes after its initial DML grants.
- Schema privilege changes also affect `ops/sql/roles.sql` and isolation checks.
