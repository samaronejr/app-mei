# OBLIGATION ENGINE AND QUEUES

## OVERVIEW
Effective-dated fiscal scheduling, queues, revenue, and document metadata; score 12, distinct fiscal engine.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Due-date resolution | `calendar.py`, `rules.py`, `holidays.py` | Effective eras and business days |
| Effective fiscal values | `parameters.py` | Missing parameters are explicit failures |
| Rolling DAS generation | `generator.py` | Twelve competences with database idempotency |
| Scheduled sweeps | `tasks.py` | Tenant-aware task integration |
| Scheduler/storage health | `heartbeat.py` | Operational readiness signals |
| Queue data and screens | `queries.py`, `views.py`, `urls.py` | Due/overdue/onboarding/threshold queues |
| Revenue threshold logic | `threshold.py` | Client revenue decisions |
| Reference models | `models/reference.py` | Types, due rules, parameters, holidays |
| Operational models | `models/operations.py`, `models/revenue.py` | Obligations and monthly revenue |
| Document metadata/keys | `models/documents.py` | Transfer UI belongs to portal |

## CONVENTIONS
- The models package re-exports its public classes from `models/__init__.py`.
- Fiscal rules and values are effective-dated reference data, not constants in views.
- Extend parameter history with a new dated row instead of editing/closing old rows.
- Generator idempotency is a database uniqueness property, not a prior existence check.
- `bulk_create(ignore_conflicts=True)` is intentional in DAS generation.
- Late-acknowledged task redelivery may repeat the same client/window.
- A raw duplicate IntegrityError would poison the enclosing task transaction.
- Generation starts no earlier than the client's opening month within the window.
- Unknown opening date uses the window start rather than silently generating nothing.
- `DueRuleCache` may be shared across clients because it holds platform reference eras.
- The bulk-create return list does not identify which conflicts were skipped.

## ANTI-PATTERNS
- Do not apply the portal's conflict-suppression ban to this idempotent generator.
- Do not replace the uniqueness constraint with read-then-insert logic.
- Do not let missing effective parameters silently become guessed statutory values.
- Do not cache tenant-specific rows in the shared rule resolver.
- Do not generate obligations for competences before a known company opening date.

## RELATED CHECKS
- `uv run pytest tests/obligations tests/ui/test_queue_views.py`
- Document transfer behavior is also exercised by `tests/portal/`.
- Reference seed migrations are replayed by the shared transactional test fixtures.
