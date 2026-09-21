# TEST SUITE CONTRACTS

## OVERVIEW
Pytest-Django integration tests with real PostgreSQL isolation; score 14, shared test infrastructure boundary.

## STRUCTURE
```text
tests/
  conftest.py   Runtime-role grants and reference-data reseeding
  support.py    MFA/login helpers and pinned rate-limit clock
  isolation/    Cross-tenant/client RLS and schema meta-tests
  portal/       Restricted-role request probes and write guards
  ui/           Rendered pages, assets, accessibility, structural scans
  accounts/     Identity, invitation, member-lifecycle behavior
  authz/        Capability and portfolio matrices
  obligations/  Fiscal engine and scheduler behavior
  core/         Contexts, checks, infrastructure contracts
  audit/        Append-only and rollback semantics
```
Other suites cover admin, clients, database setup, fiscal rules, regression, scope, security, and tenants.

## WHERE TO LOOK
| Change | Test integration |
|--------|------------------|
| Database privileges | `conftest.py` role/allow-list constants, `db/`, `isolation/` |
| Real role during requests | `portal/urls.py` probes and `portal/test_middleware.py` |
| Portal write guard | `portal/test_silent_conflict_guard.py` |
| Client-id provenance | `portal/test_client_id_provenance_guard.py` |
| Shared authentication | `support.py`; firm-page factories in `ui/factories.py` |
| Seed data after flush | `_seeded_*` autouse fixtures in `conftest.py` |

## CONVENTIONS
- Database login is `app_test`; assertions switch to unprivileged `app_runtime`.
- Session setup grants runtime DML, then restores append-only revokes and portal allow-lists.
- Keep fixture allow-lists aligned with `ops/sql/roles.sql` without broadening portal privileges.
- Database fixtures activate conditionally; pure tests should not need database connections.
- Transactional tests flush seed tables; replay production seed functions instead of copied fixtures.
- Portal transaction/role tests use `django_db(transaction=True)`; savepoints hide role leakage.
- Pin rate-limit time with `pin_rate_limit_window`, not elapsed wall-clock assumptions.
- Guard scans include planted violations, innocent controls, and nonempty-discovery assertions.
- Route contracts for not-yet-added endpoints call reverse inside tests, not during collection.
- Pytest uses test settings, strict markers/config, and warnings-as-errors from pyproject.toml.

## ANTI-PATTERNS
- Never leave isolation assertions running as table-owning BYPASSRLS `app_test`.
- Do not replace PostgreSQL with SQLite for isolation/privilege behavior.
- Do not let empty seed tables or an empty source glob masquerade as a passing guard.
- Do not generalize the portal conflict guard to obligation generation or seed migrations.
- Do not copy existing fixed sleeps as a concurrency-testing pattern; use bounded event/barrier synchronization.
- Existing timing-sensitive member-lifecycle tests are a known limitation, not an approved convention.

## COMMANDS
- `uv run pytest tests/isolation -v` exercises the database boundary.
- `uv run pytest tests/portal tests/accounts` exercises identity/portal integration.
- Django system checks are separate from pytest; run manage.py check explicitly for configuration changes.
