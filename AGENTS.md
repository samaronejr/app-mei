# PROJECT KNOWLEDGE BASE

**Generated:** 2026-09-21
**Commit:** f32f0892
**Branch:** main

## OVERVIEW
Brazilian accounting-firm/MEI multi-tenant SaaS: Python 3.13, Django 5.2, PostgreSQL RLS, Redis, Celery/beat, and allauth MFA.
Server-rendered Django templates use HTMX, Alpine CSP, and Tailwind v4; production runs Gunicorn/Caddy with OCI S3-compatible document storage.

## STRUCTURE
```text
app_mei/
├── apps/          # Domain packages; host-dispatched portal has local templates
├── config/        # Settings profiles, URL assembly, ASGI/WSGI/Celery entry points
├── templates/     # Shared and non-portal rendered pages
├── assets/        # Frontend source; Tailwind theme lives here
├── static/        # Committed CSS and pinned vendor JavaScript output
├── tests/         # Runtime-role integration tests and rendered UI checks
├── ops/           # Deployment, database roles, backup and recovery
├── docs/          # Frontend contract and accepted residual risks
└── manage.py      # Defaults to development settings
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Cross-app integration | [apps/AGENTS.md](apps/AGENTS.md) | Application package map |
| Identity, MFA, invitations | [apps/accounts/AGENTS.md](apps/accounts/AGENTS.md) | Account lifecycle |
| Audit streams | [apps/audit/AGENTS.md](apps/audit/AGENTS.md) | Transaction boundaries |
| Permissions and visibility | [apps/authz/AGENTS.md](apps/authz/AGENTS.md) | Capability grants and portal stash |
| Client registry and onboarding | [apps/clients/AGENTS.md](apps/clients/AGENTS.md) | Assignments and document normalization |
| Isolation and background work | [apps/core/AGENTS.md](apps/core/AGENTS.md) | Contexts, tasks, commands, startup checks |
| Fiscal identifiers | [apps/fiscal/AGENTS.md](apps/fiscal/AGENTS.md) | CNPJ/CPF and municipal data |
| Obligation scheduling | [apps/obligations/AGENTS.md](apps/obligations/AGENTS.md) | Effective-dated engine and queues |
| Single-client portal | [apps/portal/AGENTS.md](apps/portal/AGENTS.md) | Host dispatch, role boundary, documents |
| Tenant membership | [apps/tenants/AGENTS.md](apps/tenants/AGENTS.md) | Tenant roots and admin selection |
| Test fixtures and roles | [tests/AGENTS.md](tests/AGENTS.md) | Reseeding and shared conventions |
| Rendered UI tests | [tests/ui/AGENTS.md](tests/ui/AGENTS.md) | Real MFA fixtures and structural scanners |
| Environment policy | `config/settings/{base,dev,test,prod}.py` | Entry points select different defaults |
| Main routing | `config/urls.py`, `apps/portal/urls.py` | Portal routing is separate |
| Frontend contract | `docs/frontend.md`, `assets/css/app.css` | CSP-safe UI and Tailwind theme |
| Deployment/recovery | `ops/README.md`, `ops/RESTORE.md` | Production and restore runbooks |
| Database privileges | `ops/sql/roles.sql` | Runtime versus migration roles |
| Accepted limitations | `docs/residual-risks.md` | Recorded residual risks |

## CODE MAP
Digest-provided LSP definitions; reference counts are unmeasured unless explicitly shown.

| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `can` | Function | `apps/authz/services.py:91` | >20 | Capability and object-level decision |
| `require_can` | Decorator factory | `apps/authz/services.py:213` | Unmeasured | View gate and portal gate registration |
| `tenant_context` | Context manager | `apps/core/tenancy.py:77` | Unmeasured | Paired ContextVar/PostgreSQL scope |
| `client_context` | Context manager | `apps/core/tenancy.py:116` | Unmeasured | Portal client dimension |

## CONVENTIONS
- `uv` manages an un-packaged project; Ruff uses `ALL` with 88 columns, mypy is strict with the Django plugin.
- `manage.py` and `config/celery.py` default to dev; ASGI/WSGI default to prod.
- Frontend source is under `assets/`; generated CSS/vendor JavaScript under `static/` is committed.
- Node builds assets, but is not required in deployed images or the CI runtime for committed assets.
- Production documents use OCI S3-compatible storage; dev/test use filesystem storage.

## ANTI-PATTERNS (THIS PROJECT)
- Never combine standalone production Compose with development Compose.
- Runtime database credentials must not be superuser or `BYPASSRLS`; migrations use a distinct URL/role.
- Never run `docker compose down -v` during restore: it destroys the WAL archive.
- Never commit credentials or infer deploy/DNS/mail authorization from `.omo` drafts or proposals.
- Frontend forbids `eval`/`Function`, inline scripts, inline HTMX handlers, third-party CDNs, and `unsafe-eval`.
- Never interpolate tenant/user data into `x-*`/`hx-*` code; supply it through `data-*` attributes.
- Do not read the HttpOnly CSRF cookie from JavaScript; templates supply `csrf_token`.
- Cookies remain host-only; CSRF trusted origins must be explicit, never wildcard.

## UNIQUE STYLES
- Tailwind v4 theme tokens live in `assets/css/app.css` under `@theme`, not `tailwind.config.js`.
- Utility classes must be literal for scanning; templates must not hard-code hex colors.
- Alpine uses the CSP build and locally pinned vendors.
- Local SVG icon partials are decorative by default; icon-only controls need accessible text.

## COMMANDS
```bash
# Development services
cp .env.example .env
docker compose up -d --wait
# Make targets execute through Compose
make test
make lint  # Ruff check, format check, mypy
# Direct validation
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python manage.py check --settings=config.settings.dev
uv run python manage.py makemigrations --check --dry-run --settings=config.settings.test
# Rebuild and verify committed frontend assets
npm install
npm run build
uv run pytest tests/ui/test_asset_pipeline.py
```

## NOTES
- Other Compose-backed Make targets: `up`, `down`, `logs`, `shell`, `migrate`.
- Pytest does not invoke Django system checks; run checks explicitly for the relevant dev/test/prod profile.
- Post-migration checks matter for healed portal grants; CI also verifies the effective runtime role cannot bypass RLS.
- CI independently exercises isolation and CNPJ regression targets.
- `ops/backup.sh` and systemd units implement nightly physical/logical backups and freshness markers.
- `.omo` is workflow/session state, not application source or operational authorization.
