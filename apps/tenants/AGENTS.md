# TENANTS, MEMBERSHIP, AND ADMIN SELECTION

## OVERVIEW
Firm identity, membership scope, host tenancy, and operator selection; score 9, distinct tenant-root domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Tenant/member/invite schema | `models.py` | Roles, plans, constraints, token digests |
| Reserved slug rules | `validators.py` | Host-routing protection |
| Firm request scope | `middleware.py` | Host resolution and transaction lifetime |
| Admin tenant authorization | `admin_tenancy.py` | `authorize`, `may_operate_as` |
| Admin session context | `admin_middleware.py` | Rechecks selected tenant |
| Selection form endpoint | `admin_views.py` | Platform operator entry |
| Admin registration | `admin.py` | Invite token excluded from display |
| Membership/invite schema history | `migrations/` | Tenant/client relationship constraints |

## CONVENTIONS
- Tenant roots are deliberately outside tenant RLS; their lookup is not authorization.
- Admin operates on the platform hostname and therefore needs explicit tenant selection.
- `authorize` validates the submitted tenant against the operator's active memberships.
- Filtering the selection dropdown is convenience, not the server-side control.
- Superuser cross-tenant selection is break-glass: require a reason and audit it.
- Every admin selection is audited, including ordinary membership-based selection.
- `may_operate_as` rechecks session selections so membership revocation takes effect.
- Firm-side and client-side membership roles have different client-nullability rules.
- Invitation secrets originate from `secrets`; stored values are hashes.
- Host middleware uses token/reset discipline and renders within its scoped transaction.
- Reserved portal-style slugs are part of the boundary between firm and portal hosts.

## ANTI-PATTERNS
- Do not treat `is_staff` or a submitted tenant UUID as cross-tenant authorization.
- Do not remove a break-glass reason because the operator is a superuser.
- Do not trust a once-authorized session selection after membership changes.
- Do not expose `Invite.token` in admin or replace secure token generation with randomness guesses.
- Do not broaden root-table query access into permission to operate inside every tenant.

## RELATED CHECKS
- `uv run pytest tests/tenants tests/admin tests/isolation`
- Member lifecycle and invitation services have additional coverage in `tests/accounts/`.
- Host dispatch integration is exercised by `tests/portal/`.
