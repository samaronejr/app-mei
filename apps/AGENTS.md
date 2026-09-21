# APPLICATION KNOWLEDGE BASE

## OVERVIEW
Django domain packages and their integration seams; score 16, shared application boundary.

## STRUCTURE
```text
apps/
  accounts/     Email identity, MFA, invitation services
  audit/        Event streams and access retention
  authz/        Capability matrix and portfolio visibility
  clients/      Firm client registry and onboarding
  core/         Isolation primitives and startup checks
  fiscal/       Brazilian identifiers and reference geography
  lgpd/         Public data-subject request intake
  obligations/  Fiscal calendar, queues, revenue, documents
  portal/       Client-facing host and document transfer
  security/     CSP and endpoint-specific rate limits
  tenants/      Memberships, host resolution, admin selection
```

## WHERE TO LOOK
| Change | Integration points |
|--------|--------------------|
| New app registration | `config/settings/base.py`, app `apps.py` |
| Firm route | App `urls.py`, `config/urls.py`, root `templates/` |
| Portal route | `portal/urls.py`, `portal/templates/portal/` |
| Public privacy request | `lgpd/forms.py`, `lgpd/views.py`, `audit/models.py` |
| Credential endpoint policy | `security/ratelimit.py`, `security/middleware.py` |
| CSP response policy | `security/csp.py`, `templates/base.html` |
| Schema privilege changes | App migrations, `ops/sql/roles.sql`, `tests/conftest.py` |

## CONVENTIONS
- Domain models and services live here; most HTML lives outside apps.
- `portal` is the exception: it owns its template tree and host-specific URLconf.
- `lgpd` records intake in audit's data-subject model rather than a duplicate model.
- Rate-limit policies distinguish IP, submitted email, and authenticated tenant/user keys.
- Capability additions span matrix data, seed migrations, view gates, and tests.
- New reference-data seeds must remain compatible with transactional test reseeding.

## ANTI-PATTERNS
- Do not invent a second permission matrix inside a feature view.
- Do not make root URL resolution stand in for host-dispatched portal resolution.
- Do not add a credential endpoint without reviewing its rate-limit policy and log redaction.
- Do not blanket-grant portal access when adding a new table; its allow-lists are explicit.
