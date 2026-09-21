# AUTHORIZATION AND PORTFOLIO SCOPE

## OVERVIEW
Data-driven capability resolution and object visibility; score 12, high-reference security domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Capability catalog | `matrix.py` | Published rows/slugs |
| Persisted grants | `models.py`, `migrations/` | Seeded capability and role-grant data |
| Single decision | `services.py` | `resolve_level`, `can`, `role_of` |
| Batch page decisions | `services.py` | `granted_levels`, role-aware variant |
| View entry gate | `services.py` | `require_can`, portal gate registry |
| Visible client queryset | `portfolio.py` | `PortfolioScope`, `visible_clients` |
| Portal pre-role data | `stash.py` | Request-local `PortalStash` |
| Template permission tag | `templatetags/authz.py` | Rendering convenience only |

## CONVENTIONS
- Capability slugs are data-backed; unknown slugs raise rather than quietly deny.
- `can` refines a grant against the object, not merely a role's matrix cell.
- Role resolution observes the current client context for portal identities.
- Portfolio visibility and action authorization are separate decisions.
- Use bulk grant resolution when a page needs multiple capability decisions.
- Portal middleware gathers its stash before switching to the restricted database role.
- The restricted role cannot freely query auth/user tables to rebuild that stash.
- `require_can` registers portal gates consumed by startup checks.
- Role-grant seed changes must survive transactional fixture reseeding.

## ANTI-PATTERNS
- Never treat hidden navigation or a template tag as the destination's authorization.
- Do not turn an unknown capability into a successful or silently denied lookup.
- Do not broaden `role_of` to another client membership in the same firm.
- Do not equate access to an object with permission to perform every action on it.
- Do not replace stored grants with hard-coded role comparisons at call sites.

## RELATED CHECKS
- `uv run pytest tests/authz tests/portal/test_portal_stash.py`
- Queue authorization integration also lives in `tests/ui/`.
- Startup gate coverage is checked through `apps/core/checks.py`.
