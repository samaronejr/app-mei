# accounting-mei-saas - Work Plan

## TL;DR (For humans)

**What you'll get.** 50 executable todos covering Phases 0–1 of your solo build plan: a running Django 5.2 LTS + Postgres + Celery stack in Docker Compose, deployed to a VPS with backups you have actually restored once; a three-layer tenant isolation model whose cross-tenant denial is *proven* in CI; allauth login with mandatory TOTP for firm roles; an append-only audit log the database itself refuses to let you edit; a client registry with the report's full RBAC matrix seeded as data; alphanumeric-CNPJ-ready validators and storage; a parameterised obligation engine; and an accountant dashboard rendering a correct 12-month DAS calendar. That last item is your own §12 exit criterion, asserted end-to-end over HTTP.

**Why this approach.** Three things drove the shape of this plan beyond simply transcribing your document.

*First, the fiscal parameters are politically volatile and I verified them.* R$ 81.000 is still correct for 2026 — but increases to R$ 110.000 (2027) and R$ 140.000 (2028) are live in the Chamber and **not enacted**. Your instinct in §6 that the obligation engine must be data was right, and T-036 enforces it with a test proving a hypothetical 2027 ceiling changes behaviour with zero code edits.

*Second, alphanumeric CNPJs go into production on 31 July 2026* — days from now. That is treated as a day-one correctness requirement (T-031→T-034), not future-proofing. Critically, T-031 **refuses to implement from a blog**: it must extract the weight vectors from a Receita Federal primary source or stop and report.

*Third, the RLS design had three specific ways to fail silently*, and each is now a todo rather than a discovery. Table owners bypass RLS unless you say `FORCE`. A pooled connection leaves the session variable as `''`, and `''::uuid` throws — hence `NULLIF`. And Django's `TestCase` can make the entire isolation suite pass **vacuously** by running as owner, which is why T-014 uses `TransactionTestCase` under a non-superuser role and deliberately breaks itself three ways to prove the tests bite.

I also closed a hole your document doesn't address: Celery workers and management commands run outside the request cycle, so they never set tenant context. Under a fail-closed policy they would silently see **zero rows** rather than crash. T-013 makes that raise `MissingTenantContext` instead.

**What it will NOT do.** No client portal, documents vault, notifications, invoices, NFS-e, Asaas/Pix, bank import, reports, or connectors — those are Phases 2–4 and are listed explicitly in Scope as a Must-NOT-Have the final wave audits for. It also does **not** fork `clinic_project`; per your decision that repo is reference-only and every config here is written fresh.

**Effort.** 130–190 focused hours across 8 waves. At 15–20 h/week alongside the master's, roughly **8–12 weeks**. Waves are pause-safe: each ends in a demoable state.

**Risk.** The dominant risk is tenant isolation — in accounting, one leaked row is a churn event, so Wave 2 is entirely test-first and Wave 8's dashboard re-asserts isolation at the HTTP layer. The second risk is regulatory drift, mitigated by parameters-as-data plus both mandated regression packs. The third is that **two** todos — T-002a (`allauth.mfa` capability) and T-031 (CNPJ check-digit vectors) — are **verify-first gates** that may contradict this plan; each is instructed to stop and report rather than guess. The DAS roll direction and the DASN deadline were previously gates but are now **RESOLVED from primary sources** (see "Resolved fiscal rules") and must not be re-researched.

**Decisions made for you.** Django 5.2 LTS over 6.0 — I checked djangoproject.com directly and 5.2 LTS is supported to **April 2028** while 6.0 ends April 2027, so the "newer is safer" advice was backwards. Python 3.13 with the `uuid6` package, because stdlib `uuid7()` only arrives in 3.14. `django-allauth[mfa]` instead of `django-otp`, gated behind a verification todo. `base_manager_name = "all_objects"` so the tenant-scoped manager never becomes Django's `_base_manager` and silently breaks related lookups. And no PgBouncer at MVP, which removes the single largest RLS leak vector outright.

---

## Scope

### What this plan covers

**Phases 0–1 only** of `solo-build-plan-accounting-mei-saas.md` §8 — Foundations and Portfolio Core. Estimated 130–190 focused hours.

The deliverable at the end of this plan is the anchor document's own §12 exit criterion, hardened:

> A firm tenant with two users and three client companies sees a correct 12-month DAS calendar in staging, the cross-tenant isolation suite is green in CI, and alphanumeric CNPJs round-trip through validation, storage, search, and export.

### Explicitly OUT of scope (Must-NOT-Have)

The executor must **not** build any of the following. If a todo seems to require one, stop and report rather than improvising:

| Excluded | Belongs to |
|---|---|
| Client portal PWA, service worker, web push | Phase 2 |
| Document vault, retention rules, object storage upload flows | Phase 2 |
| Notifications (email templates, push, reminder sweeps) | Phase 2 |
| Invoice registry, NFS-e request workflow, artifact capture | Phase 3 |
| Asaas / any PSP integration, Pix, boleto, subscriptions | Phase 3 |
| OFX/CSV bank import, transactions, reconciliation | Phase 4 |
| Reports v1, portfolio KPI exports, audit **UI** | Phase 3 |
| Connector framework implementations (native/provider/portal-assist) | Phase 3+ |
| gov.br OIDC login | Post-pilot |
| eSocial payroll, NF-e/NFC-e, WhatsApp, support console | Out of MVP entirely |

Also **not** in scope: forking or copying `clinic_project`. It is **reference only** — the executor may read it to see how a pattern was solved, but must not copy files wholesale. Every config in this plan is written fresh.

### Decided constraints (do not relitigate)

| Decision | Value | Evidence |
|---|---|---|
| Framework | Django **5.2 LTS** (pin `django==5.2.*`) | djangoproject.com/download fetched 2026-07-27: 5.2 LTS extended support to **April 2028**; 6.0 ends April 2027. Migrate to 6.2 LTS after its April 2027 release. |
| Language | Python **3.13** | Supported by Django 5.2; longer runway than 3.12. |
| Database | PostgreSQL **16+** | RLS stable since 9.5; no version-specific requirement. |
| Tenancy | Shared schema, `tenant_id` on every business table | anchor §3.1 |
| Isolation | 3 layers: app manager + Postgres RLS + storage prefixes | anchor §3.1 |
| Auth | `django-allauth` incl. built-in MFA; **no** `django-otp` | See **T-002a** verify-first gate |
| Frontend | Django templates + HTMX + Alpine.js + Tailwind | anchor §2 |
| Async | Celery + Redis + django-celery-beat | anchor §2 |
| Deploy | Single VPS (Hetzner-class) + Docker Compose, Postgres local, **no PgBouncer** | user decision. Note: this removes the *pooler* leak vector but **not** all of it — Django's own `CONN_MAX_AGE` persistent connections carry the identical risk unless the GUC is transaction-local. `is_local=true` is mandatory regardless. |
| Tenant resolution | **Subdomain** (`<slug>.example.com`), session fallback in dev only | Cascades into `ALLOWED_HOSTS` (wildcard), `CSRF_TRUSTED_ORIGINS`, cookie scope, the TLS wildcard cert, and allauth email links — all written in T-004. |
| Cookie scope | **HOST-ONLY. `SESSION_COOKIE_DOMAIN = None`, `__Host-` prefixes, `CSRF_COOKIE_HTTPONLY = True`, explicit per-origin `CSRF_TRUSTED_ORIGINS` (NO wildcard)** | See the security callout below — a parent-domain cookie violates Django's own documented deployment requirement for this architecture. |

> **🔴 CORRECTION — a parent-domain session cookie is UNSAFE for this product, and an earlier draft of this plan specified it.** Django's documentation states the precondition verbatim:
> - *Security topics → Session security*: **"Django session security requires that sites are deployed such that untrusted users do not have access to any subdomains."**
> - *CSRF → Limitations*: **"Subdomains can potentially circumvent CSRF protection by setting cookies for the entire domain… ensure that subdomains are controlled by trusted users, as other vulnerabilities like session fixation also pose significant risks when subdomains are managed by untrusted parties."**
>
> This architecture gives **one subdomain per mutually-untrusted accounting firm** — exactly the configuration Django calls unsafe. With `SESSION_COOKIE_DOMAIN=.example.com`, any attacker-controlled `*.example.com` origin (via Alpine expression-context XSS, a dangling CNAME takeover, or a re-registered slug) can **read the CSRF token** (`CSRF_COOKIE_HTTPONLY` defaults to False) and **set** a `sessionid` for the parent domain. RFC 6265 gives cookies no origin integrity, so Django cannot tell which host set it. The victim then silently operates inside the **attacker's** tenant — every CNPJ and revenue figure they enter lands in the attacker's database — and **both isolation layers approve it**, because the boundary was crossed in the browser, not the database. The audit log faithfully records the *victim* as the actor.
>
> **Resolution:** host-only cookies (`SESSION_COOKIE_DOMAIN = None`), `__Host-` prefixes (browsers refuse them with a `Domain` attribute, which structurally defeats cookie tossing), `CSRF_COOKIE_HTTPONLY = True` with the token read from `{% csrf_token %}`, and an explicit per-origin `CSRF_TRUSTED_ORIGINS` list rather than `https://*.example.com`.
> **Accepted trade:** a consultant with memberships in two firms must log in twice. For tax, identity and financial data, that is the correct trade. If cross-firm SSO ever becomes a product requirement, implement it as a signed token exchange on the platform host — never a shared cookie.
> Also required: a **reserved-slug denylist** (`www`, `admin`, `app`, `api`, `mail`, `status`, `login`, …), immutable slugs after creation, and no re-registration of a released slug.

### Document precedence (read this before any todo)

`solo-build-plan-accounting-mei-saas.md` is the strategy anchor and is cited throughout — but **this plan supersedes it wherever they conflict.** The anchor contains stale or out-of-scope statements that an executor following it literally would get wrong:

| Anchor says | This plan says | Why |
|---|---|---|
| Python 3.12 | **3.13** | Longer support runway |
| `django-allauth` + `django-otp` | `django-allauth[mfa]`, no django-otp | Redundant dependency; gated by **T-002a** |
| §3.2 marks `invoices`, `documents`, `portal`, `notifications`, `banking`, `billing`, `reports`, `integrations` as "✅ in MVP" | **All out of scope** here | Those are Phases 2–4; see the Must-NOT-Have table |
| Backups "to a second provider" | Object storage from a single VPS | Single-VPS constraint |
| RLS policy `current_setting('app.tenant_id')::uuid` (fail-loud, raises) | `NULLIF(current_setting('app.tenant_id', true), '')::uuid` (fail-closed at DB, fail-**loud** at app layer via T-013) | The anchor's form raises `undefined_object` on unset; the plan's form is the verified-correct pattern |
| `SET LOCAL app.tenant_id = '<uuid>'` | `set_config('app.tenant_id', %s, true)` | `SET LOCAL` cannot take a bind parameter; string interpolation here would be an injection vector |
| §3.1 middleware sets the GUC "inside the request transaction" relying on `ATOMIC_REQUESTS` alone | `ATOMIC_REQUESTS=True` **plus** an outer `transaction.atomic()` owned by `TenantMiddleware` | `ATOMIC_REQUESTS` wraps only the **view callable**, so the GUC is gone before template rendering. A middleware-only transaction fixes that but loses rollback-on-exception. Nesting both preserves each guarantee. See T-011. |
| §4/§8 DASN-SIMEI due "last **business** day of May" | **31 May, no rolling at all** | Verified: Resolução CGSN nº 140/2018 art. 109 + RFB MEI Q&A. The deadline does not move for weekends. Confirmed by 2025, when it fell on Saturday 31 May and held. |
| §6 DAS "day 20, rolled forward" | **Confirmed correct — rolled forward** | Verified against CGSN 140/2018 art. 40 §3. Recorded here because earlier research wrongly claimed backward anticipation; the anchor was right. |
| Package manager | `uv` | anchor §9 tooling posture |
| Money / dates | `DECIMAL(14,2)` BRL; store UTC, schedule in `America/Sao_Paulo` | anchor §6 |

---

## Verification strategy

**Test strategy: TDD for the fiscal/isolation core; tests-after for CRUD, templates, and admin.**

Test-first is **mandatory** for these — write the failing test, then the implementation:

- CNPJ (alphanumeric) and CPF validators, including check digits
- Business-day rolling and the DAS due-date generator
- The R$ 81.000 threshold monitor, including the proportional opening-year rule
- The RBAC `can(user, action, obj)` matrix
- Every cross-tenant isolation assertion

Tests-after is acceptable for Django admin registration, template rendering, and plain model CRUD.

**Agent-executed QA on every todo, regardless of test strategy.** Each todo below carries a happy-path and a failure-path scenario with the exact command to run and an evidence path. No acceptance criterion in this plan requires a human to look at anything.

### Standing commands

| Purpose | Command |
|---|---|
| Lint | `uv run ruff check . && uv run ruff format --check .` |
| Types | `uv run mypy .` |
| Tests | `uv run pytest` |
| Missing migrations | `uv run python manage.py makemigrations --check --dry-run` |
| Isolation suite | `uv run pytest tests/isolation/ -v` |
| Stack up | `docker compose up -d --wait` |

Evidence convention: every QA scenario writes stdout to `.evidence/<todo-id>-<happy\|failure>.txt`. Create `.evidence/` in T-001.

**`.evidence/` is git-ignored, therefore F1 must REGENERATE evidence, not merely inspect it.** A fresh clone has no evidence files, so an "assert the file exists" audit would fail vacuously on any clean checkout or CI run. F1 re-runs each todo's acceptance commands and compares against the recorded criteria.

### Security requirements from the adversarial review (binding)

An attack-oriented pass ran 8 direct attacks against the RLS core; **all 8 were blocked** by controls this plan already specifies. Every finding below instead goes **around** the row layer. Each is a binding requirement on the todo named.

| # | Requirement | Owning todo |
|---|---|---|
| 1 | Admin tenant selector validated server-side against the operator's memberships; superuser access is break-glass with reason + `impersonation` event; MFA gates `is_staff` | T-020, T-016 |
| 2 | Host-only cookies, `__Host-` prefix, `CSRF_COOKIE_HTTPONLY`, explicit `CSRF_TRUSTED_ORIGINS`; reserved-slug denylist; immutable, non-reusable slugs | T-004, T-008 |
| 3 | Login rate limit keyed on email + IP, **never** tenant; read-endpoint limit added | T-019b |
| 4 | **Invite acceptance must bind to the invited email.** Reject acceptance while authenticated as a different user; require the accepting session's verified email to equal `Invite.email`; never silently attach a `Membership` to `request.user`. Otherwise anyone holding a forwarded link joins the firm — and invite links travel through email and ticketing systems. | T-017 |
| 5 | **`NON_TENANT_TABLES` is assertion-suppression, not protection.** `Tenant`, `Membership`, `Invite`, `AccessLog`, `DataSubjectRequest` have *no* database-layer control — only a convention to "filter on `request.user`", with no test. Add a positive control: a `for_user(user)` manager these models require, plus a per-model access test proving an unfiltered cross-tenant read is impossible through the app. | T-008, T-010, T-019a, T-019c |
| 6 | **Audit immutability does not hold against `BYPASSRLS` roles.** `app_migrator` and `app_test` own the tables and can rewrite or delete audit rows; a fixture written as `GRANT ALL ON ALL TABLES` would silently hand `app_runtime` `TRUNCATE` and break append-only without T-018's test noticing. Enforce with a rule/trigger rejecting `UPDATE`/`DELETE` for **all** roles including owners, assert `TRUNCATE` is denied, and grant column-explicitly — never `ALL`. | T-018, T-009 |
| 7 | **Audit the actions that matter.** Add events for admin tenant switch, data export, and `.all_tenants()` use. Note that the `ATOMIC_REQUESTS` savepoint **rolls back audit events written by a view that then raises** — so failed-login and denied-action events must be written outside the view transaction (they belong to `PlatformEvent`, T-018). | T-018, T-033 |
| 8 | **Composite FKs cover tenant-scoped parents only.** FKs to *global* tables (`accounts_user`, `authz_capability`) bypass RLS per PostgreSQL's documented behaviour, so a tenant can reference a foreign user's UUID. Validate `user` on `ClientAssignment` against the tenant's own membership set. | T-026 |
| 9 | **`all_objects` is an unguarded twin of `.all_tenants()`.** The grep guard covers only the latter. Extend it to `all_objects` outside `apps/core/`, and require any `RunPython` touching a tenant-scoped table to carry an explicit cross-tenant marker — `app_migrator` holds `BYPASSRLS`, so a migration is a full cross-tenant read/write primitive with no review gate. | T-012, T-010 |
| 10 | Tenant enumeration via subdomain probing is accepted as low-severity and **explicitly out of scope** for Phases 0–1; record it rather than silently ignoring it. | — |

### The two mandated regression packs

Both exist from day one (anchor §9), and CI fails if either regresses:

1. **CNPJ alfanumérico pack** — `tests/regression/test_cnpj_alfanumerico.py`. Asserts alphanumeric CNPJs survive validation, normalization, storage, admin search, queryset filtering, and CSV export.
2. **Municipality-variance pack** — `tests/regression/test_municipality_registry.py`. Phase 0–1 scope is the capability-registry **data shape and lookup only** (no NFS-e calls); it locks the schema so Phase 3 connectors cannot silently change it.

---

## Execution strategy

### Waves and dependencies

Waves run in order. Todos **within** a wave may be done in any order unless a dependency is stated.

| Wave | Theme | Depends on | Todos |
|---|---|---|---|
| W1 | Repo, toolchain, container stack | — | T-001, **T-002a**, T-002 … T-006 |
| W2 | Tenancy core + RLS isolation | W1 | **Intra-wave order is fixed: T-008 → T-009 → T-007a → T-010 → T-012 → T-007b → T-011 → T-013 → T-014.** T-007a supplies the concrete `ExampleTenantModel` that T-010 and T-012 assert against; T-007b wires the managers once `TenantScopedManager` exists. |
| W3 | Auth, audit, admin | W2 | T-016 … T-018, **T-019a, T-019b, T-019c**, T-020 |
| W4 | CI/CD, deploy, backups | W1, W2 (roles bootstrap), W3 | T-021 … T-024 |
| W5 | Client registry + RBAC-as-data | W2, W3 | T-025 … T-030 |
| W6 | Fiscal primitives (validators, registry) | **W2, W3, W5** — T-033 wires validators onto `ClientCompany` (T-025) and admin search (T-020); T-035 needs `NON_TENANT_TABLES` (T-010) | T-031 … T-035 |
| W7 | Obligation engine + DAS calendar | W5, W6 | T-036 … T-042 |
| W8 | Work queues + accountant dashboard v1 | W7 | T-043 … T-047 |
| F | Final verification wave | all | F1 … F4 |

**50 todos total.** Base T-001–T-047, plus T-002a; T-007 split into T-007a/T-007b; T-019 split into T-019a/T-019b/T-019c. There is no T-007, no T-015, and no T-019.

### Resolved fiscal rules (previously open — now settled from primary sources, encode as given)

Independent review verified both of these against Receita Federal primary sources. They are **no longer open questions**; the executor implements them as stated and does **not** re-research.

- **DAS-MEI due date rolls FORWARD.** When day 20 falls on a weekend or holiday, payment is **postponed to the next business day** — *"deverão ser pagos até o dia útil imediatamente posterior"*, Resolução CGSN nº 140/2018 art. 40 §3, corroborated by RFB's worked examples (Oct 2018 → 22nd, Dec 2020 → 21st, Nov 2021 → 22nd). **The anchor document was right and the earlier research was wrong.**
  - ⚠ **Do not confuse this with DAE-MEI** (the *employee* payroll guide), which is **anticipated backward** to the previous business day. Different regime, opposite direction. The `due_rule` data model must carry direction per obligation type precisely because these differ.
- **DASN-SIMEI is due 31 May, with NO rolling at all.** The deadline does not shift even when 31 May falls on a Saturday or Sunday — RFB's official MEI Q&A, basis Resolução CGSN nº 140/2018 art. 109. **The anchor document's "last business day of May" is WRONG**; 2025 confirmed this with a deadline that fell on Saturday 31 May and did not move. Add `none` as a valid `due_rule` direction.

### Two verify-first gates

These todos **research before implementing**. Record the finding in the todo's evidence file and, if it contradicts this plan, **stop and report** rather than proceeding on assumption.

- **T-002a** — does `allauth.mfa` actually provide TOTP + recovery codes + per-role enforcement? If not, fall back to `django-otp`. **Runs in Wave 1, immediately BEFORE the dependency pin in T-002** — a gate that fires after the decision it governs has already been committed and asserted is not a gate.
- **T-031** — confirm the CNPJ alfanumérico check-digit weight vectors against Receita Federal. Time-critical: alphanumeric CNPJs enter production **July 2026**. Note: RFB publishes exactly **one** official worked example (`12.ABC.345/01DE-35`), so requiring "≥3 official vectors" would trigger a false STOP that blocks all of Wave 6.

### Operating rules

- **WIP limit of one todo.** Finish and commit before starting the next.
- **Feature-flag nothing yet** — no connectors exist in this scope.
- Any scope change goes to a post-plan backlog, never into this plan.
- Regenerate `uv.lock` only inside the todo that changes dependencies.

---

## Todos

<!-- BEGIN TODOS -->

### Wave 1 — Repo, toolchain, container stack

#### T-001 — Initialize git repo, uv project, and evidence directory

**References**
- Target dir `/home/samarone/Documents/app_mei` — currently holds only `deep-research-report.md`, `solo-build-plan-accounting-mei-saas.md`, `.omo/`, `.codegraph/`. It is **not** a git repository.
- Reference only, do not copy: `/home/samarone/Documents/clinic_project/.pre-commit-config.yaml`, `.../Makefile`
- Anchor doc §12 "Week 1".

**Do**
- `git init` with default branch `main`. Commit the two existing markdown docs untouched as the first commit.
- `uv init --no-package` at repo root (mirrors clinic_project's `[tool.uv] package = false`).
- Write `.gitignore` covering: `.venv/`, `__pycache__/`, `*.pyc`, `.env`, `.evidence/`, `staticfiles/`, `media/`, `node_modules/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.sqlite3`.
- Create `.evidence/` with a `.gitkeep`, git-ignored except the keep file.
- Add `README.md` stating: project name, that `.omo/plans/accounting-mei-saas.md` is the active plan, and the one-command bootstrap (`cp .env.example .env && docker compose up -d --wait`).

**Acceptance criteria**
- `git -C . rev-parse --is-inside-work-tree` prints `true`.
- `git log --oneline` shows ≥1 commit; `git status --porcelain` is empty after committing.
- `test -d .evidence && test -f .evidence/.gitkeep` succeeds.
- `git check-ignore -q .env` exits 0; `git check-ignore -q README.md` exits 1.

**QA — happy**: `git -C . log --oneline && git check-ignore -v .env .evidence/x` → `.evidence/T-001-happy.txt`
**QA — failure**: `git check-ignore -q README.md; echo "exit=$?"` must print `exit=1`, proving the ignore file is not over-broad → `.evidence/T-001-failure.txt`
**Commit**: `chore(repo): initialize git, uv project, and ignore rules`

---

#### T-002 — Declare pinned dependencies in pyproject.toml

**References**
- Django **5.2 LTS** pin `django==5.2.*` — verified djangoproject.com/download 2026-07-27 (extended support April 2028; 6.0 ends April 2027).
- Python `>=3.13,<3.14`.
- Reference only: `/home/samarone/Documents/clinic_project/pyproject.toml` (shows the developer's preferred dependency set and strictness; **write fresh, do not copy**).
- `django-otp` is deliberately **absent** — conditional on **T-002a**, which runs immediately before this todo. If T-002a reports that `allauth.mfa` lacks TOTP, add `django-otp` here instead and mark the two assertions below as inverted.

**Do**
- Runtime deps: `django==5.2.*`, `psycopg[binary]>=3.2`, `django-environ`, `celery`, `django-celery-beat`, `redis`, `gunicorn`, `sentry-sdk[django]`, `django-allauth[mfa]`, `whitenoise`.
- Dev group: `ruff`, `mypy`, `django-stubs`, `pytest`, `pytest-django`, `pytest-cov`, `model-bakery`, `hypothesis`, `pre-commit`.
  - `hypothesis` is required — the CNPJ/CPF validators are property-tested (T-032).
- Set `requires-python = ">=3.13,<3.14"` and `[tool.uv] package = false`.
- Run `uv lock` and commit `uv.lock`.

**Acceptance criteria**
- `uv sync --frozen` completes with exit 0.
- `uv run python -c "import django; print(django.get_version())"` prints a `5.2.x` version.
- `uv run python -c "import sys; assert sys.version_info[:2]==(3,13)"` exits 0.
- `uv run python -c "import allauth.mfa"` exits 0.
- `grep -q "django-otp" pyproject.toml` exits 1 (must be absent).

**QA — happy**: `uv sync --frozen && uv run python -c "import django,allauth.mfa;print(django.get_version())"` → `.evidence/T-002-happy.txt`
**QA — failure**: `uv run python -c "import django_otp"` must fail with `ModuleNotFoundError`, proving the redundant dep was not silently pulled in → `.evidence/T-002-failure.txt`
**Commit**: `chore(deps): pin django 5.2 LTS, allauth mfa, and dev toolchain`

---

#### T-003 — Configure ruff, mypy, pytest, and pre-commit

**References**
- Reference only: `/home/samarone/Documents/clinic_project/pyproject.toml` lines 36–95 — the developer's established strictness (`ruff select = ["ALL"]`, `mypy strict = true`, `pytest filterwarnings = ["error"]`). Match this posture; write the config fresh.
- Ruff ignore set to carry over: `COM812`, `CPY001`, `D203`, `D213`, `FBT001`, `FBT002`, `FIX002`, `ISC001`, `TD002`, `TD003`.

**Do**
- `[tool.ruff]`: `target-version = "py313"`, `line-length = 88`, `lint.select = ["ALL"]` with the ignore set above, `fixable = ["ALL"]`.
- `[tool.ruff.lint.per-file-ignores]`: `"config/**/*.py" = ["D"]`, `"tests/**/*.py" = ["ARG","D","PLR2004","S101","SLF001"]`, `"**/migrations/*.py" = ["D","E501","RUF012","N806"]`.
- `[tool.mypy]`: `python_version = "3.13"`, `strict = true`, `plugins = ["mypy_django_plugin.main"]`, `[tool.django-stubs] django_settings_module = "config.settings.test"`. Add `ignore_missing_imports` overrides for `environ`, `celery.*`, `allauth.*`.
- `[tool.pytest.ini_options]`: `minversion = "8.0"`, `addopts = ["-ra","--strict-config","--strict-markers"]`, `filterwarnings = ["error"]`, `DJANGO_SETTINGS_MODULE = "config.settings.test"`, `testpaths = ["tests"]`.
- `.pre-commit-config.yaml` running `ruff check --fix`, `ruff format`, and `mypy`.

**Acceptance criteria**
- `uv run ruff check .` exits 0.
- `uv run ruff format --check .` exits 0.
- `uv run pre-commit run --all-files` exits 0.
- `grep -q 'select = \["ALL"\]' pyproject.toml` exits 0.
- `grep -q 'strict = true' pyproject.toml` exits 0.

**QA — happy**: `uv run ruff check . && uv run ruff format --check . && uv run pre-commit run --all-files` → `.evidence/T-003-happy.txt`
**QA — failure**: create `/tmp/slop.py` containing `import os` (unused) plus a line >88 chars, copy it into the repo, confirm `uv run ruff check` exits non-zero and names `F401`, then delete it → `.evidence/T-003-failure.txt`
**Commit**: `chore(config): ruff ALL, mypy strict, pytest strict, pre-commit`

---

#### T-004 — Django project skeleton with split settings

**References**
- Layout to mirror (reference only): `clinic_project/config/settings/{base,dev,prod,test}.py`, `config/{urls,wsgi,asgi,celery}.py`, `apps/`, `templates/`, `tests/`.
- Anchor §2 (localization) and §7 (hardening).
- Timezone rule, anchor §6: store UTC, schedule in `America/Sao_Paulo`.

**Do**
- Create `config/` with `settings/{__init__,base,dev,prod,test}.py`, `urls.py`, `wsgi.py`, `asgi.py`; `manage.py` defaulting to `config.settings.dev`; `apps/` package; `templates/`; `tests/`.
- **Define a custom user model NOW**: `apps/accounts/models.py::User(AbstractBaseUser, PermissionsMixin)` with `email` as `USERNAME_FIELD` (unique, required) and **no** `username` field, and set `AUTH_USER_MODEL = "accounts.User"` in `base.py`. This must land in the **first** migration — swapping `AUTH_USER_MODEL` after any migration exists is notoriously painful, and T-016 configures allauth for email-only login, which the stock `auth.User` only supports by auto-generating throwaway usernames.
- `base.py`: read env via `django-environ`; `LANGUAGE_CODE = "pt-br"`, `TIME_ZONE = "America/Sao_Paulo"`, `USE_TZ = True`, `USE_I18N = True`; `DECIMAL_SEPARATOR = ","`; PostgreSQL via `DATABASE_URL`; `CONN_MAX_AGE` from env, default 60.

> **⚠ `ATOMIC_REQUESTS` IS NOT A MODULE-LEVEL SETTING — writing it as one is a silent no-op.** Verified against Django 5.2.16: `ATOMIC_REQUESTS` appears **zero times** in `conf/global_settings.py`. It is exclusively a **per-database key**; `db/utils.py` does `conn.setdefault("ATOMIC_REQUESTS", False)` and `make_view_atomic` reads `connections.settings[alias]["ATOMIC_REQUESTS"]`. But `Settings.__init__` copies *every* uppercase module attribute, so a module-level `ATOMIC_REQUESTS = True` makes `settings.ATOMIC_REQUESTS` return `True` **while the connection's value stays `False`** — the view is never wrapped, no savepoint is ever created, every `Http404`/`PermissionDenied` path commits partial writes, and an assertion on `settings.ATOMIC_REQUESTS` passes green while certifying a broken system.
>
> **Set it inside the DATABASES dict**, after `django-environ`'s `env.db()` has built it:
> `DATABASES["default"]["ATOMIC_REQUESTS"] = True`
> It nests inside the outer transaction `TenantMiddleware` (T-011) owns: the middleware's block spans the view **and** template rendering to hold the tenant GUC, while `ATOMIC_REQUESTS` makes the view a savepoint that rolls back on any view exception, including ones converting to 4xx. Both are load-bearing.
>
> **Every assertion and check must read `connections["default"].settings_dict["ATOMIC_REQUESTS"]` — never `settings.ATOMIC_REQUESTS`.**
>
> **⚠ `dev.py` / `prod.py` / `test.py` must NOT reassign `DATABASES` wholesale.** `connections.settings` reads `settings.DATABASES`, so a split-settings module that rebuilds the dict from its own env var (the natural way to give `test` the `app_test` credentials from T-009) **silently discards `ATOMIC_REQUESTS`**. Mutate specific keys (`DATABASES["default"]["USER"] = ...`) instead of replacing the dict, and re-apply `ATOMIC_REQUESTS = True` in any module that does replace it.
>
> **⚠ The `django.core.checks` guard does NOT run under pytest.** `pytest-django` never invokes `DiscoverRunner.run_checks()`; system checks fire only on `manage.py check` / `migrate` / `runserver`, and `manage.py` defaults to `config.settings.dev`. So the check never validates `config.settings.test` — the one module most likely to lose the setting. The real safety net is T-011's behavioural test (a view raising `Http404` after a partial write must leave no committed rows), which goes **red** if `ATOMIC_REQUESTS` is lost under test settings. Add `--settings=config.settings.test` to a `manage.py check` invocation in CI (T-021) so the guard covers all three modules. Subdomain tenancy requires a wildcard `ALLOWED_HOSTS` entry, `CSRF_TRUSTED_ORIGINS` with the wildcard, and `SESSION_COOKIE_DOMAIN` set to the parent domain.
- `prod.py`: `DEBUG=False`, `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_CONTENT_TYPE_NOSNIFF`, `X_FRAME_OPTIONS="DENY"`, Sentry init from `SENTRY_DSN`.
- `test.py`: fast hashers, `DEBUG=False`, and the **app-role** database credentials (not superuser) — T-013 depends on this.
- `.env.example` listing every variable with safe placeholder values.
- A `/healthz` view returning 200 JSON, wired in `config/urls.py`.

**Acceptance criteria**
- `uv run python manage.py check` exits 0.
- `uv run python manage.py check --deploy --settings=config.settings.prod` reports no `security.W` issues other than ones explicitly annotated in a `# noqa`-style comment block in `prod.py`.
- `uv run python -c "from django.conf import settings; import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.dev'); django.setup(); assert settings.TIME_ZONE=='America/Sao_Paulo'; assert settings.USE_TZ"` exits 0.
- **`uv run python -c "...; from django.db import connections; assert connections['default'].settings_dict['ATOMIC_REQUESTS'] is True"` exits 0** — asserting the *connection's* value, not `settings.ATOMIC_REQUESTS`, which would pass even when the setting is inert.
- `uv run pytest tests/test_health.py` passes, asserting `/healthz` returns 200.

**QA — happy**: `uv run python manage.py check && uv run python manage.py check --deploy --settings=config.settings.prod` → `.evidence/T-004-happy.txt`
**QA — failure**: run `manage.py check --deploy --settings=config.settings.prod` with `SECRET_KEY` unset and confirm it fails loudly with `ImproperlyConfigured` rather than silently falling back to a default → `.evidence/T-004-failure.txt`
**Commit**: `feat(config): django project skeleton, split settings, pt-BR/BRT, healthz`

---

#### T-005 — Docker Compose stack and .env.example

**References**
- Anchor §2 "Docker Compose everywhere" and §8 Phase 0.
- Reference only: `clinic_project/docker-compose.yml`.
- **No PgBouncer** — deliberate (user decision); Postgres is local and unpooled, which removes the primary RLS tenant-leak vector.

**Do**
- Services: `db` (postgres:16, named volume, healthcheck `pg_isready`), `redis` (redis:7, healthcheck `redis-cli ping`), `web` (Django dev server, depends_on db+redis `service_healthy`), `worker` (celery worker), `beat` (celery beat with django-celery-beat scheduler).
- All app services share one build target and mount the source for live reload in dev.
- `POSTGRES_*` env from `.env`; expose 5432 and 6379 to localhost only.
- A `Makefile` with `up`, `down`, `logs`, `test`, `lint`, `migrate`, `shell`.

**Acceptance criteria**
- `docker compose config -q` exits 0.
- `docker compose up -d --wait` brings all five services to healthy/running within 120s.
- `docker compose exec -T db pg_isready` exits 0.
- `docker compose exec -T redis redis-cli ping` prints `PONG`.
- `docker compose exec -T web uv run python manage.py check` exits 0.
- `docker compose down -v` then `up -d --wait` succeeds from cold (proves no manual bootstrap step).

**QA — happy**: `docker compose down -v && docker compose up -d --wait && docker compose ps` → `.evidence/T-005-happy.txt`
**QA — failure**: `docker compose stop db && docker compose exec -T web uv run python manage.py migrate` must fail with a connection error (proves web genuinely depends on db, not on a cached sqlite fallback); restart db afterwards → `.evidence/T-005-failure.txt`
**Commit**: `chore(docker): compose stack for web, worker, beat, postgres 16, redis`

---

#### T-006 — Wire Celery application and beat schedule

**References**
- Anchor §2: "Celery + Redis; django-celery-beat" for reminder sweeps, due-date generation, imports, connector jobs.
- Reference only: `clinic_project/config/celery.py`.
- **Note for later**: Celery tasks run outside the request cycle and therefore outside the T-011 tenant middleware. Tenant context for tasks is handled explicitly in **T-013** — do **not** assume middleware covers workers.

**Do**
- `config/celery.py` defining the Celery app, `config.__init__` importing it, autodiscovery of `apps.*.tasks`.
- Broker and result backend from `REDIS_URL`.
- `django-celery-beat` in `INSTALLED_APPS` with `DatabaseScheduler`.
- Set `task_acks_late = True` and `task_reject_on_worker_lost = True` (jobs in this product are idempotent fiscal sweeps; losing one silently is worse than running it twice).
- A trivial `apps/core/tasks.py::ping` task returning `"pong"`, used purely as a wiring proof.

**Acceptance criteria**
- `uv run celery -A config inspect ping` (against the running stack) returns a reply from the worker.
- `docker compose exec -T web uv run python -c "from apps.core.tasks import ping; r=ping.delay(); assert r.get(timeout=20)=='pong'"` exits 0.
- `uv run python manage.py migrate django_celery_beat` applies cleanly and `PeriodicTask` is queryable.

**QA — happy**: run the `ping.delay()` round-trip above against a live stack → `.evidence/T-006-happy.txt`
**QA — failure**: `docker compose stop redis`, then `ping.delay()` must raise a connection error rather than silently returning; restart redis → `.evidence/T-006-failure.txt`
**Commit**: `feat(celery): celery app, beat database scheduler, ping wiring proof`

---

### Wave 2 — Tenancy core and RLS isolation

> This wave is the highest-risk work in the plan. In accounting, one leaked row is a churn event. Every todo here is test-first.

#### T-007a — UUIDv7 primary keys and the abstract tenant base *(Wave 2 position 3: after T-009, before T-010)*

> **Why this is split from T-007b.** T-010 (`EnableRLS`) and T-012 (`TenantScopedManager`) both need a **concrete tenant-scoped table** to assert against, and T-007b's manager wiring needs `TenantScopedManager` to exist. Keeping it as one todo made the dependency circular and both commits red. T-007a creates the tables and models; T-007b wires the managers once T-012 exists.

**References**
- Anchor §6: "UUIDv7 primary keys, generated app-side. `tenant_id` is denormalized onto every business table even where derivable — RLS policies need it locally."
- **Constraint**: Python 3.13's stdlib `uuid` has **no** `uuid7()` (added in 3.14), and Django 5.2 has no UUIDv7 field. Add the `uuid6` package to `pyproject.toml` in this todo and re-lock.

**Do**
- Add `uuid6` to runtime deps; `uv lock`.
- `apps/core/models.py`:
  - `UUIDv7PrimaryKeyModel` (abstract): `id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)`.
  - `TenantScopedModel` (abstract, extends the above): `tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, db_index=True)`, with `class Meta: abstract = True` and **`all_objects = models.Manager()`** only. The scoped manager arrives in T-007b.
- Create the concrete fixture `apps/core/tests/models.py::ExampleTenantModel(TenantScopedModel)`, registered only under `config.settings.test`. The abstract bases have no table, and no real tenant-scoped model exists until T-025 — T-010 and T-012 need something concrete to assert against.
- Every concrete tenant-scoped model declares a composite index with `tenant_id` **leading** — RLS predicates only stay index-served when `tenant_id` leads.
- **`uuid6.uuid7()` is documented as NOT thread-safe** (its monotonic counter is unguarded), and Django calls a field `default` concurrently under threaded workers, risking duplicate PKs. **DECIDED: pin gunicorn to the `sync` worker class with `--threads 1`**, asserted in T-022 and recorded in `ops/README.md`. Celery's default prefork pool is process-based and therefore safe; if the pool is ever changed to threads or gevent, this constraint must be revisited.

**Acceptance criteria**
- `uv run python -c "import uuid6; u=uuid6.uuid7(); assert u.version==7"` exits 0.
- A test asserts two successive `uuid7()` values sort ascending as strings (time-ordering property).
- `ExampleTenantModel` migrates cleanly and has a composite index with `tenant_id` leading (assert via `pg_indexes`).
- `uv run python manage.py makemigrations --check --dry-run` exits 0.

**QA — happy**: uuid7 version + ordering tests, plus the `pg_indexes` leading-column assertion → `.evidence/T-007a-happy.txt`
**QA — failure**: create an index with `tenant_id` NOT leading and assert the index-order test fails; revert → `.evidence/T-007a-failure.txt`
**Commit**: `feat(core): uuidv7 primary keys and abstract tenant-scoped base`

---

#### T-007b — Manager wiring on the tenant base *(Wave 2 position 6: after T-012)*

**References** — Django manager resolution: `Options.default_manager` and `Options.base_manager`. Depends on `TenantScopedManager` from T-012 and `ExampleTenantModel` from T-007a.

**Do**
- On `TenantScopedModel.Meta` set **`default_manager_name = "objects"`** *and* **`base_manager_name = "all_objects"`**, declaring `objects = TenantScopedManager()` alongside the existing `all_objects = models.Manager()`.

> **Why `base_manager_name` alone is not enough — this is a silent inversion.** Django resolves `_default_manager` to the **first manager declared by creation counter** unless `Meta.default_manager_name` overrides it. Declaring `all_objects` first therefore makes the *unscoped* manager the default, so everything routed through `_default_manager` (admin `get_queryset`, `dumpdata`, reverse-FK related managers, `validate_unique`) would silently run **unscoped** — the exact opposite of the intent. Setting both names makes declaration order irrelevant and states both intentions explicitly.

> **Abstract `Meta` inheritance is lossy.** A concrete model that declares its own `class Meta:` (which every tenant-scoped model does, for indexes and constraints) **replaces** the parent's Meta rather than merging it, silently dropping both manager names. Every concrete `TenantScopedModel` subclass must declare `class Meta(TenantScopedModel.Meta):`. The meta-test checks *all* concrete subclasses, because a single-model test would not catch this.

**Acceptance criteria**
- **A meta-test iterating EVERY concrete subclass of `TenantScopedModel`** asserts, per model: `_meta.default_manager_name == "objects"`, `_meta.base_manager_name == "all_objects"`, `_default_manager` is a `TenantScopedManager`, and **`type(model._base_manager) is models.Manager`** (identity, not `isinstance` — `TenantScopedManager` subclasses `Manager`, so `isinstance` would pass vacuously). Covers `ExampleTenantModel` now and T-025+ models automatically.
- `uv run python manage.py makemigrations --check --dry-run` exits 0 (manager changes must not generate a migration).

**QA — happy**: all-subclass manager meta-test → `.evidence/T-007b-happy.txt`
**QA — failure**: run **two** mutations, each must turn the meta-test red, then revert — (a) remove `default_manager_name` and confirm `_default_manager` becomes the unscoped `all_objects`; (b) give `ExampleTenantModel` a bare `class Meta:` not inheriting `TenantScopedModel.Meta` and confirm both names are lost → `.evidence/T-007b-failure.txt`
**Commit**: `feat(core): wire scoped default manager and unscoped base manager`

---

#### T-008 — Tenant, Membership, and Invite models

**References**
- Anchor §3.2 (`tenants` app), §12 Week 1.
- Report RBAC matrix (`deep-research-report.md` lines 49–66) — roles: platform admin, firm owner, staff accountant, firm operations admin. Client-side roles arrive in T-025.

**Do**
- `apps/tenants/models.py`:
  - `Tenant` — id (UUIDv7), `name`, `slug` (unique, used for subdomain resolution), `plan`, `is_active`, timestamps. **`Tenant` is NOT tenant-scoped** and gets **no** `app.tenant_id` RLS policy — it is the tenancy root.
  - `Membership` — FK user, FK tenant, `role` (choices: `owner`, `staff_accountant`, `operations_admin`), `is_active`. Unique together (user, tenant).

> **BOOTSTRAP CIRCULARITY — do not put `Membership` behind an `app.tenant_id` policy.**
> The T-011 middleware must read `Membership` to *discover* which tenant the request belongs to, which happens **before** `app.tenant_id` can be set. If `Membership` were RLS-scoped on `app.tenant_id`, that lookup would return zero rows under the fail-closed policy and **no user could ever log in**. `Tenant`, `Membership`, and `Invite` therefore go in `NON_TENANT_TABLES` (T-010) and are protected at the **application layer** by filtering on `request.user` instead. Add an explicit test that a user's `Membership` lookup succeeds with **no** tenant context set — that is the login path, and it must work.
  - `Invite` — FK tenant, `email`, `role`, `token` (unguessable, ≥32 bytes urlsafe), `expires_at`, `accepted_at`. Token stored **hashed**, never in plaintext.
- Register all three in Django admin (T-020 refines).

**Acceptance criteria**
- Migrations apply cleanly on a cold database.
- A test asserts `Invite.token` is never stored in plaintext: create an invite with a known token and assert the raw value does not appear in `Invite.objects.values_list()` output.
- A test asserts `Membership` uniqueness raises `IntegrityError` on duplicate (user, tenant).
- A test asserts `Tenant` has **no** RLS policy (it is the root) — queried via `pg_policies`.

**QA — happy**: `uv run pytest tests/tenants/ -v` → `.evidence/T-008-happy.txt`
**QA — failure**: attempt a duplicate `Membership` insert and capture the `IntegrityError` → `.evidence/T-008-failure.txt`
**Commit**: `feat(tenants): tenant, membership, and hashed-token invite models`

---

#### T-009 — PostgreSQL role split: app role without BYPASSRLS

**References**
- Verified research: **table owners bypass RLS by default**; superusers always bypass it. If the app connects as owner or superuser, every policy in this plan is inert.
- Anchor §3.1: "The app connects under a role without `BYPASSRLS`; migrations run under a separate role."
- Reference only (prior art for idempotent role bootstrap): `clinic_project` commits `chore(config): compose postgres 16, settings, idempotent role bootstrap` and `fix(db): allow owner resolver role ownership transfers`.

**Do**
- An **idempotent** bootstrap SQL script `ops/sql/roles.sql`, placed in the `db` service's `/docker-entrypoint-initdb.d/` **in this todo** so T-005's cold-boot criterion (`down -v && up -d --wait` with no manual steps) keeps holding. Re-runnable safely:
  - `app_migrator` — owns the schema, runs migrations. **Grant it `BYPASSRLS`.**

> **WHY `app_migrator` NEEDS `BYPASSRLS`.** Table owners are subject to policies once `FORCE ROW LEVEL SECURITY` is set (T-010). Without `BYPASSRLS`, any `RunPython` data migration that iterates or backfills a tenant-scoped table would see **zero rows and silently succeed** — a backfill that appears to work and does nothing. This is the single most dangerous silent failure in the whole isolation design. `app_migrator` bypasses; `app_runtime` never does.

> **TEST-ROLE SPLIT.** `pytest-django` must `CREATE DATABASE`, and `TransactionTestCase` flushes with `TRUNCATE` — neither is available to a bare non-superuser. (Note: `TRUNCATE` requires its own privilege; it is *not* blocked by T-018's revoked `DELETE`, and it is documented as **not subject to RLS**.) Create a third role `app_test` with `CREATEDB` that **owns** the test database; test settings connect as `app_test`, then `SET ROLE app_runtime` for the assertions themselves.
>
> **Verified**: `SET ROLE` *does* cause RLS to apply as the target role — policy evaluation uses the current user, and `app_runtime` is neither table owner nor `BYPASSRLS`, so policies bite. The isolation suite is therefore meaningful, not vacuous.
>
> **⚠ BUT the grants do not carry over, and this WILL fail as originally specified.** The test database's tables are owned by **`app_test`**, so the `ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator` above does **not** apply to them — `app_runtime` would have *zero* table privileges there and the suite would die with `permission denied`, never reaching an RLS assertion. Compounding it, the test database is created fresh from `template1`, which carries none of the main database's ACLs. Fix both:
> - Add `ALTER DEFAULT PRIVILEGES FOR ROLE app_test IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;`
> - Apply the grants in a `pytest` `django_db_setup` fixture that runs **after** migrations, so they land on the actual test tables regardless of template state. Note `ALTER DEFAULT PRIVILEGES` is **per-database and only affects objects created afterwards**, so the fixture must issue an explicit `GRANT ... ON ALL TABLES IN SCHEMA public` — the default-privileges statement alone is inert for already-migrated tables.
> - **The blanket fixture GRANT must then RE-APPLY T-018's REVOKE**, or it silently restores `UPDATE`/`DELETE` on `audit_event` and T-018's append-only test either fails or passes vacuously. Re-run `REVOKE UPDATE, DELETE ON audit_event FROM app_runtime` as the last statement of the fixture.
> - **Grant `app_test` `BYPASSRLS` too.** It runs the test-suite migrations, and under `FORCE ROW LEVEL SECURITY` a `RunPython` data migration would otherwise see zero rows in tests while working correctly in production under `app_migrator` — a prod/test divergence in exactly the most dangerous direction. Assertions remain meaningful because they execute under `SET ROLE app_runtime`, which has neither ownership nor `BYPASSRLS`.
> - `SET ROLE` must be **reset before teardown** (`RESET ROLE` in fixture teardown), because `TransactionTestCase` flushes via `TRUNCATE` as the *current* role and `app_runtime` cannot truncate `app_test`-owned tables.
>
> **Also note a legitimate environment difference**: `ALTER ROLE app_runtime SET app.tenant_id TO ''` applies at **login**, not after `SET ROLE`. So in tests the GUC is *absent* (`NULL`) while in production it is *empty string* (`''`). Both fail closed through `NULLIF(current_setting(..., true), '')`, but T-014 must assert **both** paths explicitly — otherwise the suite only ever exercises one of them.
  - `app_runtime` — LOGIN, **NOSUPERUSER**, **NOBYPASSRLS**; granted `CONNECT`, `USAGE` on schema, and `SELECT/INSERT/UPDATE/DELETE` on tables. Explicitly **not** the table owner.
  - `ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator ... GRANT ... TO app_runtime` so new tables are usable without a manual grant.
  - `ALTER ROLE app_runtime SET app.tenant_id TO ''` — establishes the empty-string default that the T-010 policy treats as "no context".
  - **`GRANT app_runtime TO app_test;` and `GRANT app_migrator TO app_test;`**

> **⚠ WITHOUT THE GRANT, `SET ROLE` FAILS AND WAVE 2 CANNOT RUN AT ALL.** PostgreSQL permits `SET ROLE <r>` only when the current role is a **member** of `<r>` (or is a superuser). `app_test` is neither. Verified empirically on PostgreSQL 16: `SET ROLE app_runtime` as `app_test` without membership returns `permission denied to set role "app_runtime"`. Every test in T-014 depends on that `SET ROLE`, so the entire isolation suite — the centrepiece of this plan — dies on its first statement. This is a one-line omission with a total-blocker consequence.
- Django settings: **`dev`/`prod` connect as `app_runtime`**; **`test` connects as `app_test` and issues `SET ROLE app_runtime` per test** (see the test-role split below — `app_runtime` has no `CREATEDB`, so wiring `config.settings.test` directly to it would kill `pytest-django` at `CREATE DATABASE` before a single test ran). A separate `DATABASE_MIGRATION_URL` using `app_migrator` is used only by `manage.py migrate`.
- Document in `ops/README.md` that running migrations as `app_runtime` will fail by design.

**Acceptance criteria**
- `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='app_runtime'` returns `(false, false)`.
- A test asserts the Django default connection's `current_user` is `app_runtime`, not `postgres`.
- Re-running `ops/sql/roles.sql` a second time exits 0 (idempotency proof).

**QA — happy**: run the `pg_roles` query and the `current_user` test → `.evidence/T-009-happy.txt`
**QA — failure**: attempt `CREATE TABLE` as `app_runtime` and confirm it is refused; and confirm `SELECT current_user` is never `postgres` → `.evidence/T-009-failure.txt`
**Commit**: `feat(db): split app_runtime and app_migrator roles, no bypassrls`

---

#### T-010 — Reusable EnableRLS migration operation

**References**
- Verified research, all three traps encoded here:
  1. `ENABLE ROW LEVEL SECURITY` alone is insufficient — **`FORCE ROW LEVEL SECURITY`** is required or the owner bypasses it.
  2. Fail-closed requires the `missing_ok` form `current_setting('app.tenant_id', true)`.
  3. A recycled connection leaves the GUC as `''`, and `''::uuid` raises SQLSTATE `22P02` — so `NULLIF(..., '')` is mandatory.
- PostgreSQL `CREATE POLICY` docs.

**Do**
- `apps/core/migrations/operations.py` defining `EnableRLS(model_name, tenant_field="tenant_id")`, a custom `migrations.Operation` whose `database_forwards` emits:
  ```sql
  ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
  ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
  CREATE POLICY {table}_tenant_isolation ON {table}
      FOR ALL
      USING ({tenant_field} = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
      WITH CHECK ({tenant_field} = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
  ```
  and whose `database_backwards` drops the policy and disables RLS.
- `WITH CHECK` is **not** optional — without it a tenant could INSERT or UPDATE a row *into* another tenant.
- Apply `EnableRLS` to every concrete `TenantScopedModel` created so far.
- Maintain `apps/core/rls.py::NON_TENANT_TABLES`, an explicit allow-list of tables that legitimately have no policy. **Seed it with the complete literal list below** — an under-populated list turns CI red in a later wave with no todo instructing the fix:

```python
NON_TENANT_TABLES = {
    # tenancy roots — see T-008 bootstrap-circularity callout
    "tenants_tenant", "tenants_membership", "tenants_invite",
    # django / auth / infra
    "auth_*", "django_*", "django_celery_beat_*",
    # custom user model from T-004 — matches NONE of the globs above
    # ("account_*" requires the literal prefix; "accounts_user" does not match)
    "accounts_*",
    # allauth (T-016) — NOT covered by the auth_* or django_* globs
    "account_*", "mfa_*", "socialaccount_*", "usersessions_*",
    # platform-level reference and control data
    "authz_capability", "authz_rolegrant",              # T-027
    "audit_accesslog",                                   # T-019
    "fiscal_municipality", "fiscal_municipalitycapability",  # T-035
    "obligations_fiscalparameter", "obligations_obligationtype",  # T-036
    "obligations_holiday",                               # T-039
    "obligations_schedulerheartbeat",                    # T-041
}
```

> **This set contains GLOB PATTERNS, so T-014's inverse check must match with `fnmatch`, not `in`.** `"auth_user" in NON_TENANT_TABLES` is `False` — an exact-membership check would turn the isolation suite red on `django_migrations`, `auth_user`, `account_emailaddress` and every other globbed table the moment T-014 lands. T-014 must use `fnmatch.fnmatchcase(table, pattern)` across the set. State this in both todos.

> **Every later todo that creates a table MUST add it to this list in the SAME commit** if it is not tenant-scoped. T-014's inverse check makes an omission a red build, and "every commit leaves the tree green" makes that a blocked wave. This applies to **T-016, T-019a, T-019c, T-026, T-027, T-029, T-035, T-036, T-039, and T-041** — note especially:
> - **T-019c** creates `DataSubjectRequest`. Place it at `apps/audit/models.py::DataSubjectRequest`, platform-level (a DSR arrives pre-authentication with no tenant), and add `"audit_datasubjectrequest"`.
> - **T-026** creates an **implicit M2M join table** for `Tag ↔ ClientCompany`. Django auto-generates it with **no tenant column and therefore no RLS** — a genuine cross-tenant write vector. Use an explicit `through=` model inheriting `TenantScopedModel` rather than allow-listing it.
> - **T-029** creates the onboarding-item *template* table (platform-level) alongside the tenant-scoped `OnboardingItem`.

> **Cross-tenant FK writes are NOT blocked by RLS.** PostgreSQL documents that referential-integrity checks (FK and unique constraints) **bypass row security** to preserve integrity. So a `WITH CHECK` policy asserting only `tenant_id = current` will happily accept a `ClientAssignment` whose `client_id` points at *another tenant's* row — the FK check confirms existence without RLS filtering. This is both a data-integrity hole and an existence oracle. **DECIDED — composite FK, not app-layer validation**, on every tenant-crossing FK. Concretely: `ClientCompany` gains `UniqueConstraint(fields=["tenant","id"], name="clientcompany_tenant_id_uniq")`, and `ClientAssignment` (T-026), `Obligation` (T-041) and `MonthlyRevenue` (T-042) each reference it with a composite `FOREIGN KEY (tenant_id, client_id) REFERENCES clients_clientcompany (tenant_id, id)` added via `RunSQL` in their own migration. The database then makes a cross-tenant reference structurally impossible rather than relying on a check someone can forget. Each of those three todos repeats this requirement in its Do and asserts it.

**Acceptance criteria**
- After migrate, `SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='<a tenant table>'` returns `(true, true)`.
- `SELECT COUNT(*) FROM pg_policies WHERE tablename='<a tenant table>'` returns ≥1.
- The policy text contains both `NULLIF` and `, true)` — asserted by querying `pg_policies.qual`.
- Migration reverse (`migrate <app> <previous>`) drops the policy cleanly.

**QA — happy**: apply migrations, then dump `pg_class` + `pg_policies` rows for a tenant table → `.evidence/T-010-happy.txt`
**QA — failure**: in a scratch transaction, `SET app.tenant_id = ''` then `SELECT ... ::uuid` on the raw expression **without** `NULLIF` to demonstrate the `22P02` error the policy avoids → `.evidence/T-010-failure.txt`
**Commit**: `feat(db): reusable EnableRLS operation with FORCE and fail-closed policy`

---

#### T-011 — Tenant-resolving middleware inside the request transaction

**References**
- Anchor §3.1: middleware issues `SET LOCAL app.tenant_id` inside the request transaction, with `ATOMIC_REQUESTS=True` (set in T-004).
- Verified research: use `set_config(name, value, true)` (transaction-local). Plain `SET` is **unsafe** with `CONN_MAX_AGE > 0` — the value survives on the pooled connection and leaks to the next request.

> **⚠ `ATOMIC_REQUESTS` ALONE DOES NOT WORK — this is the correction that makes the whole design function.**
> `ATOMIC_REQUESTS` wraps **only the view callable**. Middleware runs *outside* that transaction, so a `set_config(..., true)` issued in `process_request` lands in its own autocommit transaction and is **discarded before the view ever runs** — the GUC would be empty exactly when it matters. Worse, `TemplateResponse` rendering happens in the *response* phase, also outside the transaction, so any lazy queryset evaluated during template rendering would execute with no tenant context and return **zero rows** — producing a silently empty dashboard rather than an error.
> **The middleware must therefore own the transaction itself**, not rely on `ATOMIC_REQUESTS`.

**Do**
- `apps/tenants/middleware.py::TenantMiddleware.__call__` structured so that a **single `transaction.atomic()` block encloses both `get_response(request)` and template rendering**:
  1. Resolve tenant from the subdomain `slug` (session fallback in dev only), using the **unscoped** `Membership` read permitted by T-008.
  2. Reject with 403 if the authenticated user has no active `Membership` for the resolved tenant.
  3. Open `transaction.atomic()`.
  4. Inside it, `SELECT set_config('app.tenant_id', %s, true)` with the tenant UUID as a **bound parameter** — never string-interpolated (this is why `SET LOCAL` cannot be used; it takes no placeholders).
  5. Set the `contextvars.ContextVar` for T-012 and `request.tenant`, **capturing the returned token** and resetting it in a `finally` block (see below).
  6. Call `get_response(request)`, then if the response has an unrendered `render` method, call `response.render()` while still inside the atomic block. (Django's `_get_response` normally renders already; this is a defensive no-op for a `TemplateResponse` produced by inner response middleware.)
  7. **If the response is a `StreamingHttpResponse`/`FileResponse`, raise `TypeError`** (see below).
  8. Exit the block; the GUC is discarded automatically.

> **⚠ KEEP `ATOMIC_REQUESTS = True` AS WELL — the two nest correctly and each fixes what the other cannot.**
> - `ATOMIC_REQUESTS` wraps only the **view callable**, so on its own the GUC is gone before rendering. That was the original bug.
> - But a *middleware-owned* transaction alone loses rollback-on-exception: Django applies `convert_exception_to_response` **inside** our block, turning a view exception into a 500 **response** that returns normally — so `atomic()` sees a clean exit and **commits the partial writes**. Checking `status_code >= 500` does not fix this either, because `Http404`, `PermissionDenied`, `SuspiciousOperation` and `BadRequest` convert to **4xx** and would still commit, even though `ATOMIC_REQUESTS=True` would have rolled them back.
> - **Solution: both.** The middleware opens the OUTER `atomic()` (holding the GUC across the view *and* rendering); `ATOMIC_REQUESTS=True` makes the view a nested **savepoint** that rolls back on **any** view exception regardless of the status code it converts to. The GUC is set on the outer transaction before the savepoint, so a savepoint rollback never disturbs it.
> - Therefore **T-004 sets `ATOMIC_REQUESTS = True`**, and `django.core.checks` must fail startup if it is `False` *or* if `TenantMiddleware` is absent — both are now load-bearing.

> **⚠ STREAMING RESPONSES EVALUATE OUTSIDE THE TRANSACTION — so they are forbidden here.** A `StreamingHttpResponse`'s iterator is consumed by the WSGI server *after* all middleware returns, by which time the block has exited and the GUC is gone; every lazy query in the generator returns **zero rows**, producing a file that downloads successfully and is empty. Materializing it inside the block is also unsafe, because a rolled-back transaction would raise `TransactionManagementError` and mask the original error. **DECIDED: tenant-scoped views must not return streaming responses; the middleware raises `TypeError` on one.** T-033's CSV export builds its payload in memory inside the request and returns a plain `HttpResponse` with `Content-Disposition: attachment`. This decision lives here, not in T-033 — T-033 is four waves later and cannot gate a Wave-2 acceptance criterion.

> **⚠ ContextVar MUST be reset in `finally`.** Gunicorn `sync` workers reuse the same thread across requests, so a `ContextVar` set and not reset leaks the previous request's tenant into the next one — a cross-tenant read at the app layer even though RLS would still block the database. Use the `token = var.set(...)` / `var.reset(token)` pattern in `try/finally`, never a bare `set()`.

- For anonymous/unresolved requests, explicitly set the GUC to `''` so a reused connection can never inherit a previous request's value.
- Register **after** `AuthenticationMiddleware`.
- Keep `ATOMIC_REQUESTS = True` as set in T-004's DATABASES dict, and add a `django.core.checks` error that fires if `TenantMiddleware` is absent from `MIDDLEWARE` **or** if `connections["default"].settings_dict["ATOMIC_REQUESTS"]` is not `True`. Both are load-bearing, and the check must read the connection's value — reading `settings.ATOMIC_REQUESTS` would pass vacuously.

**Acceptance criteria**
- A test asserts that during view execution, `SELECT current_setting('app.tenant_id', true)` equals the resolved tenant UUID.
- **A test renders a `TemplateResponse` whose context holds an unevaluated (lazy) queryset of a tenant-scoped model, and asserts the rendered HTML contains the rows.** This is the regression test for the rendering-outside-transaction failure; it must fail if `response.render()` is moved outside the atomic block.
- A test asserts that **after** the response, on the same connection, the GUC is `''` or NULL.
- A test asserts an anonymous request leaves the GUC empty, not stale from a prior authenticated request on the same connection.
- A test asserts `Membership` is readable with **no** tenant context (the login path from T-008).
- **A test asserts a view raising an exception leaves NO committed rows** — proving the `ATOMIC_REQUESTS` savepoint rolls back and that a 500 does not commit partial writes.
- **A test asserts the ContextVar does not leak across two sequential requests on the same thread** (request A as tenant A, then an anonymous request, then assert the var is unset — not tenant A).
- A view returning a `StreamingHttpResponse` under a resolved tenant raises `TypeError` — asserted, not merely documented.
- **A test asserts a view raising `Http404` or `PermissionDenied` after a partial write leaves NO committed rows** — the case a `status_code >= 500` check would have missed.
- A `django.core.checks` error is raised if `TenantMiddleware` is missing from `MIDDLEWARE` **or** if `ATOMIC_REQUESTS` is `False`.

> **All tests in this todo must use `TransactionTestCase` (or `pytest.mark.django_db(transaction=True)`).** Under plain `TestCase` the whole test already runs inside a transaction, so the middleware's `atomic()` degrades to a **savepoint** — and `SET LOCAL` values persist to the *enclosing* transaction on savepoint RELEASE rather than being discarded. The "GUC is empty after the response" assertion would then fail, or worse, pass for the wrong reason while state leaks between tests.

**QA — happy**: run the middleware GUC tests → `.evidence/T-011-happy.txt`
**QA — failure**: change `set_config(..., true)` to `..., false)`, run the "GUC empty after response" test, confirm it FAILS (proving the test genuinely detects leakage); revert → `.evidence/T-011-failure.txt`
**Commit**: `feat(tenants): tenant-resolving middleware with transaction-local set_config`

---

#### T-012 — TenantScopedManager and the audited escape hatch

**References**
- Anchor §3.1: "Unscoped access requires an explicit `.all_tenants()` call — rare, intentional, and grep-auditable."
- **T-007b** (which runs *after* this todo, at position 6) sets `base_manager_name = "all_objects"` and `default_manager_name = "objects"`. Neither is in effect here — do not assert them in this todo.

**Do**
- `apps/core/managers.py::TenantScopedManager` — `get_queryset()` filters by the `ContextVar` tenant set in T-011.
- When **no** tenant is in context, `get_queryset()` returns `none()` (fail-closed), matching RLS behaviour rather than contradicting it.
- Provide `TenantScopedManager.all_tenants()` returning the unfiltered queryset, and require every call site to carry a `# ALL_TENANTS_OK: <reason>` comment.
- Add a ruff-friendly guard test that greps the codebase for `.all_tenants(` and fails if any occurrence lacks the adjacent marker comment.

> **Assert against the manager INSTANCE, not `Model.objects` — it does not exist yet at this position.** T-012 runs at Wave-2 position 5; `objects = TenantScopedManager()` is declared in **T-007b at position 6**, and Django does not auto-create `objects` once any manager is declared, so `ExampleTenantModel.objects` would raise `AttributeError`. In this commit, attach the manager under a distinct name — `scoped = TenantScopedManager()` on `apps/core/tests/models.py::ExampleTenantModel` — and assert through that. The `_default_manager`/`_base_manager` identity assertions belong to T-007b, where they already live.
>
> **⚠ DECLARE THE ContextVar IN THIS COMMIT, AND SET BOTH LAYERS IN THE FIXTURE.** No earlier todo declares it — T-011 (position 7) and T-013 (position 8) both *set* "the ContextVar for T-012", but T-012 runs at position **5**. Declare `apps/core/tenancy.py::current_tenant_id: ContextVar[UUID | None]` here; T-011 and T-013 then import it rather than inventing their own.
>
> **The manager reads the ContextVar, NOT the GUC.** A fixture that establishes tenant context with only a raw `set_config` leaves the ContextVar unset, so `get_queryset()` returns `none()` and criteria 1 and 2 both observe **zero** — indistinguishable, and satisfied by a manager hard-coded to `return self.none()`. The fixture must set **both**: `token = current_tenant_id.set(tenant_a.id)` (reset in teardown) **and** `transaction.atomic()` + raw `SELECT set_config('app.tenant_id', %s, true)`, because RLS is already forced from T-010 at position 4.

**Acceptance criteria**
- With tenant A in **both** layers and rows seeded for A and B: `ExampleTenantModel.scoped.count()` **equals A's row count and is strictly greater than zero**, and contains no B rows. (Stated this way so it cannot pass at zero.)
- With the ContextVar unset but the GUC still set to A: `ExampleTenantModel.scoped.count() == 0` — proving the manager keys on the ContextVar, fail-closed, independently of RLS.
- With tenant A in both layers, `ExampleTenantModel.all_objects.count()` equals A's row count (RLS bounds it) and is strictly greater than zero — proving `all_objects` is reachable and unfiltered by the ContextVar.
- The `.all_tenants(` marker-comment guard test passes, and excludes its own source file from the grep so it cannot self-match.

**QA — happy**: run the manager scoping tests → `.evidence/T-012-happy.txt`
**QA — failure**: run **two** mutations, each must turn the suite red, then revert — (a) add a temporary `.all_tenants()` call with no marker comment and confirm the guard test FAILS; (b) **replace `get_queryset` with an unconditional `return self.none()`** and confirm criterion 1 goes red — this is the mutation that proves criterion 1 is not vacuous → `.evidence/T-012-failure.txt`
**Commit**: `feat(core): tenant-scoped default manager with audited all_tenants escape hatch`

---

#### T-013 — Tenant context for Celery tasks and management commands

**References**
- **This closes a real hole.** T-011's middleware only runs in the request/response cycle. Celery workers, `manage.py` commands, and beat-scheduled sweeps run **outside** it, so `app.tenant_id` is never set. With the fail-closed policy from T-010, those jobs would see **zero rows** and fail silently rather than loudly — which is worse than a crash.
- Anchor §2 lists reminder sweeps, due-date generation, and imports as Celery work — all of which are tenant-scoped.

**Do**
- `apps/core/tenancy.py::tenant_context(tenant_id)` — a context manager that opens an explicit transaction, issues `set_config('app.tenant_id', <uuid>, true)`, sets the `ContextVar`, and restores both on exit.

> **⚠ RESTORING THE GUC MUST BE EXPLICIT — savepoint semantics will not do it for you.** When `tenant_context` is entered while a transaction is *already* open (a `PlatformTask` looping over tenants, an eager Celery task inside a request, or any nested use), `atomic()` degrades to a **savepoint**, and PostgreSQL **merges** a `SET LOCAL` value into the parent transaction on `RELEASE SAVEPOINT` rather than discarding it. So after the block exits, the GUC still holds the *last* tenant — a cross-tenant leak in exactly the loop the `PlatformTask` pattern encourages. On entry, capture the current value with `current_setting('app.tenant_id', true)`; on exit, `set_config` it **back explicitly** in a `finally`. Reset the `ContextVar` by token in the same `finally`.
>
> Note the acceptance test below would *not* catch this on its own, because each iteration sets the value correctly on entry — the leak is only visible **after** the loop. Assert the post-loop state explicitly.
- A Celery base task class `TenantTask` requiring a `tenant_id` first argument and entering `tenant_context` automatically. Tasks that are genuinely cross-tenant must subclass `PlatformTask` and are expected to iterate tenants explicitly, entering `tenant_context` per tenant.
- A `TenantAwareBaseCommand` for management commands, taking a `--tenant` argument.
- **Fail loudly**: a tenant-scoped task invoked with no tenant context raises `MissingTenantContext` rather than returning empty results.

**Acceptance criteria**
- A test runs a `TenantTask` eagerly for tenant A and asserts it sees only tenant A's rows.
- A test asserts invoking a tenant-scoped task **without** tenant context raises `MissingTenantContext` (not an empty queryset).
- A test asserts a `PlatformTask` iterating two tenants observes each tenant's rows in turn and never both at once.

**QA — happy**: run the task tenancy tests → `.evidence/T-013-happy.txt`
**QA — failure**: invoke a tenant-scoped task with `tenant_id=None`, capture the `MissingTenantContext` traceback → `.evidence/T-013-failure.txt`
**Commit**: `feat(core): explicit tenant context for celery tasks and management commands`

---

#### T-014 — Isolation test suite and RLS coverage meta-test

**References**
- Anchor §3.1 and §7: "the cross-tenant-denial test running in CI on every PR".
- Verified research — **the vacuous-pass trap**: Django's `TestCase` wraps each test in a transaction and may run as owner/superuser, so RLS tests can pass without exercising a single policy. Use `TransactionTestCase` and connect as `app_runtime` (T-009 wired `config.settings.test` to that role).

**Do**
- `tests/isolation/test_cross_tenant.py` using `TransactionTestCase`:
  1. Tenant A context cannot read tenant B rows (assert count 0, not just "different").
  2. **No** tenant context returns **zero** rows — the fail-closed proof. Assert **both** shapes: the GUC **absent entirely** (`current_setting` → NULL, which is what tests see, because `ALTER ROLE ... SET` applies at login and not after `SET ROLE`) and the GUC set to the **empty string** (which is what production sees). Only asserting one leaves the other path unexercised.
  3. `WITH CHECK` enforcement: inserting a row carrying tenant B's id while tenant A is in context is rejected.
  4. An HTTP-level test: authenticated as a tenant-A user, requesting a tenant-B object's URL returns 404/403, never the object.
- `tests/isolation/test_rls_coverage.py` — the meta-test. Enumerate every concrete model inheriting `TenantScopedModel`, resolve `db_table`, and assert each has `relrowsecurity` **and** `relforcerowsecurity` true.
  - **Assert policy *shape*, not merely existence.** A bare "≥1 policy exists" check passes for a `FOR SELECT`-only policy that leaves INSERT and UPDATE completely unguarded, and would also pass if someone later "fixed" a broken app by adding a permissive `USING (true)` policy. Assert, per table: `pg_policies.cmd = 'ALL'`, `qual IS NOT NULL`, **`with_check IS NOT NULL`**, and that neither expression is the literal `true`.
  - Assert every tenant-scoped table's `tenant_id` column is `NOT NULL`.
  - **Inverse check**: enumerate every table in the database and assert each is either tenant-scoped-with-policy or **matches an entry in `NON_TENANT_TABLES` via `fnmatch.fnmatchcase`** (T-010). The set contains glob patterns, so a plain `in` membership test would fail on `django_migrations`, `auth_user`, `accounts_user` and every other globbed table. A new business table with no `tenant_id` and no allow-list entry must fail — otherwise the gap is invisible.
> **⚠ THE "NOT SUPERUSER" GUARD IS NOT ENOUGH — `app_test` now has `BYPASSRLS` (T-009), and it is not a superuser.** Reproduced on PostgreSQL 16: a role that is *owner + `BYPASSRLS` + `rolsuper = false`* sees **all rows** even with `FORCE ROW LEVEL SECURITY` set and no tenant GUC — while the guard happily passes. Two independent mechanisms drop the `SET ROLE` that makes assertions meaningful:
> - `TransactionTestCase._post_teardown` closes every initialized connection **after every test** (Django 5.2.16 `test/testcases.py`), so a session-scoped `SET ROLE` fixture is dead from test #2 onward.
> - `SET ROLE` issued *inside* a transaction is reverted by `ROLLBACK` — and T-011's exception tests and case 3 below both force rollbacks.
>
> Therefore: make `SET ROLE app_runtime` a **function-scoped autouse fixture** that depends on the `db`/`transactional_db` fixture (so it runs after Django's test-case setup and resets before `TRUNCATE` teardown, which must execute as the owning role), issued outside a transaction, and replace the guard with a **per-test** assertion of
> `SELECT current_user` **and** `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user` → `('app_runtime', false, false)`.
>
> **⚠ `SET ROLE` MASKS THE LOGIN ROLE, so a superuser-connection mutation can never turn this suite red.** Verified on PostgreSQL 16: connecting as `postgres` and then `SET ROLE app_runtime` makes RLS apply as `app_runtime` — `current_user` reports `app_runtime`, `rolsuper`/`rolbypassrls` report false, and the denial cases still pass. The old mutation (c) was therefore unachievable. Also assert `SELECT session_user` and record it, so the *login* identity is visible in the evidence even though it is deliberately not what governs RLS.

> **⚠ THE DENIAL CASES MUST BYPASS THE ORM MANAGER.** Written as `Model.objects…`, cases 1–2 are satisfied by `TenantScopedManager`'s ContextVar filter **even with RLS switched off entirely** — proving Layer 1 and asserting nothing about Layer 2. Execute cases 1–3 through `Model.all_objects` or a raw `connection.cursor()` so an inert policy cannot be masked by application-layer filtering.

**Acceptance criteria**
- `uv run pytest tests/isolation/ -v` passes, all four cross-tenant cases plus the coverage meta-test.
- The suite fails if `FORCE ROW LEVEL SECURITY` is removed from any tenant table.
- The suite fails if a new `TenantScopedModel` is added without an `EnableRLS` migration.
- The role guard fails if tests are run as a superuser.

**QA — happy**: `uv run pytest tests/isolation/ -v` → `.evidence/T-014-happy.txt`
**QA — failure**: run **five** mutations, each must turn the suite red, then revert each — (a) drop `FORCE` on one table, (b) add a new `TenantScopedModel` with no `EnableRLS`, (c) **revoke `app_runtime` membership from `app_test` (`REVOKE app_runtime FROM app_test`)** so `SET ROLE` fails with `permission denied to set role` — this replaces the old superuser mutation, which was unachievable because `SET ROLE` masks the login role, (d) **remove the `SET ROLE app_runtime` fixture so assertions run as `app_test` (owner + `BYPASSRLS`)** — the per-test role assertion must catch this, (e) **`ALTER TABLE … DISABLE ROW LEVEL SECURITY`** — denial cases 1–2 must go red, not merely the metadata meta-test → `.evidence/T-014-failure.txt`
**Commit**: `test(isolation): cross-tenant denial suite and RLS coverage meta-test`

---

### Wave 3 — Auth, audit, admin

#### T-002a — VERIFY-FIRST: confirm allauth.mfa provides TOTP *(executes in Wave 1, immediately BEFORE T-002; listed here for narrative continuity with Wave 3's auth work)*

**References** — Anchor §2 proposes `django-allauth + django-otp`. Research claims (medium confidence) that `django-allauth[mfa]` now ships TOTP, recovery codes, and WebAuthn, making `django-otp` redundant. `clinic_project` still uses `django-otp==1.7.*`. This is an **unvalidated assumption** and must be settled before T-016.

**Do** — Read the installed allauth's own docs/source (`allauth/mfa/`) and confirm: (a) TOTP enrolment + verification, (b) recovery codes, (c) a way to *require* MFA per user group. Record the finding verbatim in the evidence file. **If any of the three is missing, STOP and report** — the fallback is adding `django-otp` and this plan's dependency list changes.

**Acceptance criteria** — `.evidence/T-002a-verify.txt` exists and states, with file/line citations from the installed package, whether TOTP, recovery codes, and per-role enforcement exist. `uv run python -c "from allauth.mfa import totp"` exits 0 if the claim holds.
**QA — happy**: import + docs citation captured → `.evidence/T-002a-happy.txt`
**QA — failure**: if TOTP is absent, capture the ImportError and the STOP report → `.evidence/T-002a-failure.txt`
**Commit**: `docs(auth): verify allauth mfa capability before wiring`

---

#### T-016 — allauth authentication with mandatory MFA for firm roles

**References** — Anchor §7: "MFA (TOTP) mandatory for all firm-side roles from day one; optional for MEI clients in v1." **T-002a's** finding governs the implementation.

**Do** — Configure allauth for email+password (no username), email verification mandatory, and `allauth.mfa` with TOTP. Add middleware or a mixin that **forces** any user holding an active `Membership` (i.e. firm-side) into MFA enrolment before reaching any tenant view; client-side users are exempt in v1. Never expose whether an email exists on failed login.

**Acceptance criteria** — A firm-side user without MFA is redirected to enrolment on every tenant URL. A firm-side user with MFA reaches the view. Login with a wrong password returns an identical response body/timing bucket to login with an unknown email. Session cookie is `Secure`/`HttpOnly`/`SameSite=Lax` under prod settings.
**QA — happy**: `uv run pytest tests/accounts/ -v` → `.evidence/T-016-happy.txt`
**QA — failure**: attempt to reach a tenant URL as a firm user with MFA disabled; assert redirect, not 200 → `.evidence/T-016-failure.txt`
**Commit**: `feat(accounts): allauth email login with mandatory TOTP for firm roles`

---

#### T-017 — Invite issuance and acceptance flow

**References** — T-008 `Invite` model (hashed token). Anchor §3.2 `accounts` app. Report RBAC matrix: only platform admin, firm owner, and operations admin may create users.

**Do** — Issue invite (role-gated via T-028's `can()` once available; until then gate on `Membership.role`), email a signed single-use link, and on acceptance create the `User` + `Membership` atomically and mark `accepted_at`. Enforce expiry and single use. Log both issuance and acceptance to `audit.Event` (T-018).

**Acceptance criteria** — Accepting a valid invite creates exactly one `Membership`. Re-accepting the same token fails. An expired token fails. A staff_accountant cannot issue an invite. The raw token never appears in the database or in logs.
**QA — happy**: full issue→accept round trip → `.evidence/T-017-happy.txt`
**QA — failure**: replay an accepted token and an expired token; both must be rejected → `.evidence/T-017-failure.txt`
**Commit**: `feat(accounts): single-use expiring invite issuance and acceptance`

---

#### T-018 — Append-only audit.Event with database-enforced immutability

**References** — Anchor §6: "`audit.Event` is append-only (database grants allow INSERT/SELECT only)". Critical-action list (anchor §6 / report line 252): logins, MFA changes, role changes, assignment changes, connector connect/disconnect, invoice attempts, DAS confirmations, declaration submissions, exports, impersonation. Only the actions whose features exist in Phases 0–1 are wired now; the enum carries the full list so later phases add no migration.

> **⚠ A TENANT-SCOPED AUDIT TABLE CANNOT RECORD PRE-TENANT EVENTS — this design as originally written made login impossible.** `audit_event` is tenant-scoped, so its RLS policy carries `WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)`. At **login**, `TenantMiddleware` has not resolved a tenant yet, so the GUC is `''` and the check evaluates NULL → **the INSERT is rejected**. Verified empirically on PostgreSQL 16. The same kills anonymous **invite acceptance** (T-017) and **DSR intake** (T-019c). T-018's own acceptance criterion — "logging in produces exactly one event" — was therefore unachievable.
>
> **Resolution: two append-only tables, split by whether a tenant is attributable.**

**Do** — Two models in `apps/audit/models.py`, both append-only:

1. **`Event`** — **tenant-scoped, RLS-protected** (T-010). Tenant FK (NOT NULL), actor FK, `action` enum (covering the full critical-action list so later phases add no migration), `object_type`, `object_id`, `metadata` JSONB, `created_at`. Used for every action attributable to a resolved tenant: role changes, assignment changes, connector connect/disconnect, DAS confirmations, declaration submissions, exports, impersonation. **Writes must occur inside a resolved tenant context** — where an action is attributable but the request has not yet resolved one (e.g. audit emitted right after authentication), wrap the write in `tenant_context(membership.tenant_id)` per T-013.
2. **`PlatformEvent`** — **platform-level, NOT tenant-scoped**; add `"audit_platformevent"` to `NON_TENANT_TABLES` in this commit. Nullable tenant FK (recorded when known, for later correlation), actor FK (nullable), same action enum, IP, `created_at`. Used for identity events that occur **before or without** tenant resolution: login success, login failure, MFA enrolment/change, invite issue, invite acceptance, DSR submission.

- Both migrations issue `REVOKE UPDATE, DELETE ON <table> FROM app_runtime`, granting only `INSERT, SELECT`.
- Document the split inline: it exists because a fail-closed RLS policy would otherwise silently reject exactly the security events an incident investigation needs most.

**Acceptance criteria** — For **both** tables: `INSERT` and `SELECT` succeed as `app_runtime`; `UPDATE` and `DELETE` both raise `InsufficientPrivilege`. `audit_event` is tenant-scoped and carries an RLS policy (T-010); `audit_platformevent` is in `NON_TENANT_TABLES` and the T-014 coverage meta-test still passes. **Logging in produces exactly one `PlatformEvent` and zero `Event` rows.** A tenant-attributable action performed inside a resolved tenant context produces exactly one `Event`. **An `Event` INSERT attempted with an empty tenant GUC is rejected** — asserted explicitly, so the reason for the split cannot be forgotten and silently undone.
**QA — happy**: insert + select, then dump grants from `information_schema.table_privileges` → `.evidence/T-018-happy.txt`
**QA — failure**: attempt `UPDATE audit_event SET action='x'` and `DELETE FROM audit_event` as `app_runtime`; both must raise → `.evidence/T-018-failure.txt`
**Commit**: `feat(audit): append-only event log with revoked update and delete grants`

---

#### T-019a — Marco Civil access-log stream, six-month retention

**References** — Anchor §6: "a **separate access-log stream retained six months** satisfies the Marco Civil requirement and is documented apart from business-audit retention." Report line 252 confirms the six-month statutory duty. Deliberately **not** the same table as `audit.Event` — different purpose, retention, and access policy.

**Do** — `apps/audit/models.py::AccessLog` — timestamp, **`tenant` FK (nullable)**, user (nullable), IP, user agent, method, path, status code. Written by lightweight middleware **registered BEFORE `TenantMiddleware` in `MIDDLEWARE`** — if it sat inside `TenantMiddleware`'s transaction, the rollback on a 5xx (T-011) would erase the access-log record for exactly the failed request an incident investigation needs, defeating the Marco Civil obligation. Sitting outside, it still reads `request.tenant`, which `TenantMiddleware` has already attached. A Celery beat job purges rows older than 180 days. `docs/retention.md` stating the regimes side by side: fiscal artifacts ≥5 years, access logs 6 months, business audit per policy.

> **Why `tenant` is present but the table is NOT RLS-scoped.** Without a tenant column you cannot answer "who accessed my firm's data" — a request a firm will make and Marco Civil contemplates. But the log must also capture **pre-authentication and anonymous** requests, which have no tenant; a fail-closed RLS policy would reject those INSERTs and silently lose exactly the records an incident investigation needs. So: nullable `tenant` FK, table listed in `NON_TENANT_TABLES` (T-010), scoping applied at the application layer in admin and queries.

**Acceptance criteria** — A request produces exactly one `AccessLog` row carrying the resolved tenant (or NULL when anonymous). The purge deletes a row aged 181 days and retains one aged 179 days. `docs/retention.md` exists and names both regimes. The beat schedule contains the purge task. `audit_accesslog` is in `NON_TENANT_TABLES` and T-014 still passes.
**QA — happy**: authenticated request → row with tenant; anonymous request → row with NULL tenant; purge with seeded 179/181-day rows → `.evidence/T-019a-happy.txt`
**QA — failure**: seed a 181-day row, run purge, assert count 0 while the 179-day row survives → `.evidence/T-019a-failure.txt`
**Commit**: `feat(audit): access-log stream with six-month retention purge`

---

#### T-019b — Per-tenant rate limiting

**References** — Anchor §7 lists "per-tenant rate limiting" in the non-negotiable hardening baseline. Cheap now, painful to retrofit.

> **🔴 DO NOT KEY THE LOGIN LIMIT ON TENANT — an earlier draft did, and its acceptance criterion asserted the bypass as a feature.** Credentials are **global**: T-004 makes `email` the `USERNAME_FIELD` on a platform-wide `accounts.User`, so the same password works on every subdomain. A limit keyed `(tenant_id, ip)` lets an attacker rotate the Host header to mint a fresh bucket per tenant — **5 × N attempts/minute** across N firms — and the correct-password signal is readable from *any* subdomain, because during a login POST the user is not yet authenticated and `TenantMiddleware`'s membership 403 cannot yet apply. The old criterion "*tenant B's login still succeeds after tenant A's limit is exhausted*" was literally a test that the bypass works.

**Do** — Add **`django-ratelimit`** to `pyproject.toml` and re-lock. Apply:
- **Login / password-reset — TENANT-INDEPENDENT**, whichever trips first: **5/m per `email`** (global, across all subdomains) **and** **20/m per IP** (global). The tenant is **not** part of either key.
- Authenticated write endpoints (POST/PUT/PATCH/DELETE): **60/m** keyed `(tenant_id, user_id)` — tenant-keying is correct *here*, because the user is authenticated and tenant-resolved.
- **Read endpoints: 120/m per `(tenant_id, user_id)`** — closes the DoS vector where unlimited dashboard GETs each hold a transaction open through template rendering against `sync --threads 1` workers.
- Client IP must be derived from a **trusted-proxy** configuration; document whether `X-Forwarded-For` is trusted, or the limit is either spoofable or collapses every user into one bucket behind the reverse proxy.
Use the Redis cache from T-005. Exceeding a limit returns **429** with a pt-BR message.

**Acceptance criteria** — The 6th login attempt for one email within a minute returns 429 **even when each attempt targets a different tenant subdomain** — this is the anti-bypass assertion and replaces the old one. The 21st login from one IP within a minute returns 429 regardless of email. The 61st write in a minute returns 429. The 121st read returns 429. Limits are read from settings, not hardcoded.
**QA — happy**: drive 5 then 6 login attempts for one email, assert 200-class then 429 → `.evidence/T-019b-happy.txt`
**QA — failure**: exhaust one email's login limit on `firma-a`, then attempt the **same email** on `firma-b` and assert it is **still 429** — proving the key is tenant-independent. Also assert two *different* emails do not share a bucket → `.evidence/T-019b-failure.txt`
**Commit**: `feat(security): per-tenant rate limiting on auth and write endpoints`

---

#### T-019c — LGPD documentation and data-subject-rights intake

**References** — Anchor §7 names these non-negotiable from day one: lawful-basis mapping, the named *encarregado* (DPO), the incident runbook with the ANPD path, and DSR intake. Report lines 256–260 give the controller/operator split.

**Do** — `docs/lgpd.md` recording: the controller/operator split (operator for firm-service data, controller for platform telemetry/security), lawful basis per workflow (legal obligation and contract performance carry most tax workflows; consent only where genuinely required), the named encarregado with contact, and a one-page incident runbook including the ANPD notification path. Add a DSR intake route at **`/lgpd/solicitacao/`** with fields: requester name, CPF, relationship (holder / representative), request type (access / correction / deletion / portability / restriction), and free-text detail — persisted as a `DataSubjectRequest` row and emailed to the encarregado. Include the "cannot delete yet — legal/tax retention applies" response template.

> **PII-in-audit tension, resolved deliberately.** `audit.Event` is append-only, but LGPD grants erasure rights. Storing CPF/CNPJ **values** in `Event.metadata` would create an unerasable PII store. `Event.metadata` therefore stores **references** (`object_type` + `object_id`) only — never document numbers or names.

**Acceptance criteria** — `docs/lgpd.md` exists and names the encarregado and the ANPD path. `POST /lgpd/solicitacao/` with valid data creates exactly one `DataSubjectRequest` and sends one email. **A test asserts no `Event.metadata` value anywhere matches a CPF- or CNPJ-shaped pattern.**
**QA — happy**: submit a DSR, assert row + email → `.evidence/T-019c-happy.txt`
**QA — failure**: write an `Event` whose metadata contains a CPF-shaped string; assert the PII-shape test FAILS; remove it → `.evidence/T-019c-failure.txt`
**Commit**: `feat(lgpd): dsr intake route, lawful-basis docs, and pii-shape guard`

---

#### T-020 — Tenant-safe Django admin

**References** — Anchor §1: "the platform-admin console, back-office CRUD, and ops tooling come nearly free from Django admin." **Risk**: admin uses `_base_manager` in places, and staff users could otherwise see across tenants.

> **⚠ THE ADMIN CANNOT SEE TENANT-SCOPED ROWS AT ALL UNDER THIS DESIGN — resolve it here.** Admin is served on the *platform* hostname, so `TenantMiddleware` resolves no tenant and sets `app.tenant_id = ''`. Two independent layers then return nothing: `_default_manager` is now the scoped manager (T-007b) and yields `none()`, **and** the RLS policy fails closed at the database. So every tenant-scoped changelist renders **empty**, and T-020's acceptance criterion ("a non-superuser staff user sees only their tenant's rows") can only pass vacuously — an empty list is trivially "only their tenant's rows".
>
> **Decision for Phase 0–1**: the admin requires an explicit **tenant selector**. Choosing a tenant must set **both layers** — `app.tenant_id` for the request *and* the `ContextVar` the scoped manager reads. Setting only the GUC fixes the database layer and leaves `_default_manager` still returning `none()`.
>
> **⚠ AND IT MUST SPAN RENDERING.** A `with tenant_context(...)` block wrapped around `ModelAdmin.get_queryset()` alone does **not** work: the queryset is lazy and is evaluated while the changelist **template renders**, by which time the block has exited and both layers are gone — the identical failure T-011 exists to fix, reappearing in the admin. Implement the selector as **middleware or a `ModelAdmin` mixin that wraps the whole request/response including rendering**, exactly as `TenantMiddleware` does, or force evaluation inside the block. A silent empty changelist is the failure mode.

> **The acceptance criterion below must not be satisfiable by an empty list.** "Sees only their tenant's rows" is trivially true of zero rows — which is precisely what a broken selector produces.

> **🔴 THE SELECTOR IS A CROSS-TENANT READ PRIMITIVE UNLESS IT IS AUTHORIZED — this is the single highest-severity finding of the adversarial review.** As originally written, T-020 said *which tenant* to select but never *which tenants a given staff user may select*. `Tenant` is deliberately outside RLS (T-008), so the option list is `Tenant.objects.all()` — every firm on the platform — and the chosen tenant arrives as a **request parameter**, so filtering the dropdown in the template changes nothing. Textbook IDOR. And it is invisible to both isolation layers *by design*: the GUC says tenant B, so RLS correctly returns tenant B's rows; the ContextVar says tenant B, so the manager returns them too. **Nothing is violated.** One `is_staff` account would read every firm's CNPJs, CPFs, revenue and audit stream.
>
> **Required:**
> - Validate the submitted tenant **server-side** against `Tenant.objects.filter(membership__user=request.user, membership__is_active=True)` for non-superusers — the option list is a UI convenience, the check is the control.
> - Cross-tenant access by `is_superuser` is a **break-glass** path: it must capture a reason and emit an `Event` with `action="impersonation"` (already in T-018's enum) recording actor, target tenant, and reason.
> - **Emit an audit event on every selection**, not only on break-glass. A platform operator reading a firm's client registry is processing personal data under LGPD; an `AccessLog` URL row is not an adequate record of *which tenant* was accessed.
> - **MFA must gate the admin console too.** T-016 forces MFA on users holding an active `Membership`; a platform operator may have none, which would leave the highest-privilege surface in the product unprotected. Gate on `is_staff` as well as membership. Cross-tenant listing is **not available** in this phase — which is the least-privilege answer and avoids introducing a `BYPASSRLS` web path. Non-tenant models (`Tenant`, `AccessLog`, parameter tables) list normally because they carry no policy.

**Do** — Register `Tenant`, `Membership`, `Invite`, `Event`, `AccessLog`. For tenant-scoped models, `get_queryset()` uses the tenant selected above and returns an explicit "select a tenant" empty state when none is chosen — never a silent empty table. Admin is reachable only by `is_staff` platform operators over the platform hostname, never a tenant subdomain. `Event` and `AccessLog` are `has_add_permission = has_change_permission = has_delete_permission = False`.

**Acceptance criteria** — With a tenant selected and rows seeded for two tenants, a scoped changelist shows **at least one row belonging to the selected tenant (strictly > 0) and zero rows belonging to any other** — stated so an empty list fails rather than passes. Switching the selector to the other tenant changes the visible set. With **no** tenant selected, the changelist renders an explicit "select a tenant" state, not a silent empty table. Audit models are read-only in admin. Admin URLs 404 on a tenant subdomain.
**QA — happy**: `uv run pytest tests/admin/ -v` → `.evidence/T-020-happy.txt`
**QA — failure**: attempt to POST an edit to an `Event` via admin; assert 403 → `.evidence/T-020-failure.txt`
**Commit**: `feat(admin): tenant-scoped admin with read-only audit models`

---

### Wave 4 — CI/CD, deploy, backups

#### T-021 — GitHub Actions CI pipeline

**References** — Anchor §7: the cross-tenant-denial test runs "in CI on every PR". Standing commands in the Verification strategy section.

**Do** — Workflow on push and PR to `main`: service containers for Postgres 16 and Redis; steps for `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy`, `makemigrations --check --dry-run`, `pytest` with coverage, a dedicated `pytest tests/isolation/` step, and the named CNPJ regression pack step from T-034.

> **⚠ THE ROLES BOOTSTRAP WILL NOT RUN IN CI AS SPECIFIED.** T-009 places `ops/sql/roles.sql` in the `db` container's `/docker-entrypoint-initdb.d/`, which works for local Compose. But GitHub Actions **starts service containers before the repository is checked out**, so the file does not exist when Postgres initialises — `app_runtime`, `app_migrator`, and `app_test` are never created and every test step fails. Add an explicit step after checkout and before any test step:
> `psql "$SUPERUSER_DATABASE_URL" -v ON_ERROR_STOP=1 -f ops/sql/roles.sql`
> This is why T-009's script must be idempotent — it now runs in two different ways.

> **`.evidence/` is git-ignored and is not produced by a plain `pytest` run**, so an "upload `.evidence/`" step would upload nothing. Either drop that upload, or have the workflow run the todo QA commands that generate it. Do not claim an artifact the job never creates.

**Acceptance criteria** — A push runs **every configured step** (assert against the workflow file's step list rather than a hardcoded count, which goes stale as steps are added) and reports green. Deliberately breaking each of lint / types / a missing migration / an isolation test turns CI red. The roles bootstrap step runs before the first test step. The isolation step asserts, inside the job, that the **effective** role after `SET ROLE` (`SELECT current_user`) is `app_runtime` and that **that** role has `rolsuper = false` and `rolbypassrls = false`. It must **not** assert this of the *login* role — `config.settings.test` connects as `app_test`, which deliberately holds `CREATEDB` and `BYPASSRLS`, so asserting against `session_user` would fail by design.
**QA — happy**: capture a green run's step list → `.evidence/T-021-happy.txt`
**QA — failure**: push a branch with an unformatted file and a missing migration; capture the red run → `.evidence/T-021-failure.txt`
**Commit**: `ci(github): lint, types, migrations check, tests, isolation suite`

---

#### T-022 — Staging deploy to VPS via Docker Compose

**References** — User decision: single VPS (Hetzner-class) + Compose, Postgres local, **no PgBouncer**. Anchor §11 mitigation: "infrastructure-as-code".

**Do** — `docker-compose.prod.yml` with pinned image tags, **gunicorn pinned to `--worker-class sync --threads 1`** (required by T-007a: `uuid6.uuid7()` is not thread-safe and is used as a PK default), whitenoise for static, a reverse proxy terminating TLS (Caddy or nginx + certbot), and restart policies. A deploy script/workflow that builds, pushes, pulls on the VPS, runs `migrate` as `app_migrator`, `collectstatic`, and restarts with zero-downtime-ish rolling. Secrets come from the VPS environment or a secrets file **never** in git. Document the one-time VPS provisioning in `ops/README.md`.

**Acceptance criteria** — A deploy run ends with `/healthz` returning 200 over HTTPS on the staging host. The running gunicorn process is asserted to use `sync` workers with `--threads 1` (grep the compose command and assert the running arg list). `git grep` finds no secret literal. Migrations run as `app_migrator` and the app process connects as `app_runtime` (assert via a `/healthz` field reporting `current_user`).
**QA — happy**: deploy, then `curl -fsS https://<staging>/healthz` → `.evidence/T-022-happy.txt`
**QA — failure**: deploy with a deliberately wrong `DATABASE_URL`; assert the container fails its healthcheck rather than serving 500s silently → `.evidence/T-022-failure.txt`
**Commit**: `chore(deploy): compose-based staging deploy to vps with tls`

---

#### T-023 — Backups: PITR plus nightly logical dump

**References** — Anchor §7: "point-in-time recovery (WAL archiving) + nightly logical dumps to a second provider + a monthly restore rehearsal. A backup you haven't restored is a hypothesis."

**Do** — Enable WAL archiving on the Postgres container to a mounted volume, plus a nightly `pg_dump -Fc` uploaded to S3-compatible storage (R2/B2) under a dedicated credential with write-but-not-delete permission. Retain 30 dailies. A beat task or host cron triggers it and records success to a log the health endpoint can surface.

**Acceptance criteria** — A dump file appears in object storage after a manual trigger and is non-empty. `pg_restore --list` on the dump succeeds. WAL files accumulate in the archive directory. A failed upload produces a loud error, not a silent skip.
**QA — happy**: trigger dump, list object, `pg_restore --list` → `.evidence/T-023-happy.txt`
**QA — failure**: point the uploader at bad credentials; assert non-zero exit and an error log entry → `.evidence/T-023-failure.txt`
**Commit**: `chore(ops): wal archiving and nightly logical dump to object storage`

---

#### T-024 — Execute one full restore rehearsal

**References** — Anchor §7 and §11. This todo exists because an untested backup is not a backup.

**Do** — Restore the most recent nightly dump into a **throwaway** database, run `manage.py migrate --check` against it, and assert row counts for `Tenant`, `Membership`, and a tenant-scoped table match the source within the expected delta. Write `ops/RESTORE.md` as a step-by-step runbook including expected duration, and schedule a monthly reminder.

**Acceptance criteria** — The rehearsal completes and `ops/RESTORE.md` exists with the actual measured duration recorded. Row-count comparison output is captured. The restored database passes the T-014 isolation suite (proving RLS policies survive a dump/restore cycle — they are schema objects and must).
**QA — happy**: full restore + row counts + isolation suite against the restored DB → `.evidence/T-024-happy.txt`
**QA — failure**: attempt restore from a truncated dump file; assert `pg_restore` fails loudly → `.evidence/T-024-failure.txt`
**Commit**: `docs(ops): restore runbook with executed rehearsal evidence`

---

### Wave 5 — Client registry and RBAC-as-data

#### T-025 — ClientCompany model

**References** — Anchor §3.2 (`clients` app), §6 (CNPJ/CPF stored normalized `CHAR(14)`/`CHAR(11)`). Report ER model `CLIENT_COMPANY`. Depends on T-032 validators for field validation, but the model may land first with validators wired in T-033.

**Do** — `ClientCompany(TenantScopedModel)`: `legal_name`, `trade_name`, `cnpj` (`CHAR(14)`, unique per tenant, alphanumeric-ready), `cpf` (`CHAR(11)`, nullable), `municipality_ibge_code`, `state`, `main_cnae`, `opened_on`, `status` (`onboarding`/`active`/`suspended`/`closed`), `is_mei` flag, `has_employee` flag (default False — anchor §4 keeps payroll out of scope but the flag sizes demand). `EnableRLS` migration. Composite index `(tenant_id, status)` and `(tenant_id, cnpj)`, tenant leading.

**Acceptance criteria** — Migration applies with RLS enabled+forced. Uniqueness of `cnpj` is **per tenant**, not global — proven by inserting the same CNPJ under two tenants successfully. Indexes exist with `tenant_id` leading (assert via `pg_indexes`).
**QA — happy**: create clients under two tenants with the same CNPJ; both succeed → `.evidence/T-025-happy.txt`
**QA — failure**: duplicate CNPJ within one tenant must raise `IntegrityError` → `.evidence/T-025-failure.txt`
**Commit**: `feat(clients): client company registry with per-tenant cnpj uniqueness`

---

#### T-026 — Client assignments and tags

**References** — Anchor §3.1: "staff accountants seeing *assigned clients only* … is application-layer." Report ER `CLIENT_ASSIGNMENT`.

**Do** — `ClientAssignment(TenantScopedModel)`: FK client, FK user, `role` (`primary`/`support`), unique (client, user), **plus the composite `FOREIGN KEY (tenant_id, client_id)` from T-010** so a cross-tenant assignment is structurally impossible. `Tag(TenantScopedModel)` related to `ClientCompany` through an **explicit `through=` model inheriting `TenantScopedModel`** — never Django's implicit M2M join table, which would be created with no tenant column and therefore no RLS, reopening the cross-tenant write vector. All three models get `EnableRLS`. Provide `ClientCompany.objects.assigned_to(user)`.

**Acceptance criteria** — A staff accountant's `assigned_to` queryset excludes unassigned clients of the same tenant. Firm owners are not restricted by assignment. Duplicate assignment raises. Assignment changes emit an `audit.Event`. **A raw SQL insert of a `ClientAssignment` whose `client_id` belongs to another tenant is rejected by the composite FK** (not merely invisible). The `Tag` join table appears in the T-014 coverage meta-test as tenant-scoped with a policy.
**QA — happy**: assignment scoping tests → `.evidence/T-026-happy.txt`
**QA — failure**: duplicate assignment insert raises `IntegrityError` → `.evidence/T-026-failure.txt`
**Commit**: `feat(clients): assignments, tags, and assigned-only querysets`

---

#### T-027 — RBAC permission matrix as data

**References** — Anchor §3.1: "Implement the matrix as data (a permissions table) behind a single `can(user, action, obj)` entry point, so the whole matrix is unit-testable and future role tweaks don't touch view code." Source matrix: `deep-research-report.md` **lines 51–66 = 16 capability rows × 6 roles = 96 cells** (line 49 is the header, line 50 the separator — counting them yields the wrong total).

**Do** — `apps/authz/models.py`: `Capability` (slug), `RoleGrant` (role, capability, level).

> **The level enum must be able to express the source matrix.** The report uses **seven** distinct values, not four: `✅`, `❌`, `Limited`, `View / confirm`, `Initiate / approve`, `Own actions only`, `Ticket only`. A four-value enum cannot encode three of them, making "match the matrix exactly" unsatisfiable. Use:
> `full` (✅) · `none` (❌) · `limited` · `view_confirm` · `initiate_approve` · `own` (Own actions only) · `ticket_only`
> Document the semantic of each in the model docstring — T-028 resolves `limited`/`own` against the object.

> **Role coverage — DECIDED.** `Membership.role` (T-008) offers only `owner`/`staff_accountant`/`operations_admin`, so three of the matrix's six roles have no assignment path. **Resolution: `can()` short-circuits `True` for `User.is_superuser` before the `Membership` lookup**, which covers the report's *Platform admin* row without a migration against the already-committed T-008 model. `client_owner` and `client_collaborator` are **seed-only** in this phase with no assignment path until the Phase 2 portal — state this in the migration docstring so their absence is not mistaken for a bug.

- Seed via data migration transcribing the matrix exactly. Rows for out-of-scope capabilities (invoices, DAS submit) are seeded now with no call sites yet.

**Acceptance criteria** — A parametrised test asserts all **96** cells match the report matrix exactly, including all seven level values. Seeding is idempotent. Changing a grant level requires no code change. `authz_capability` and `authz_rolegrant` are added to `NON_TENANT_TABLES` **in this commit** (T-010).
**QA — happy**: the full 96-cell matrix test passes → `.evidence/T-027-happy.txt`
**QA — failure**: flip one seeded cell, confirm the matrix test fails naming that cell; revert → `.evidence/T-027-failure.txt`
**Commit**: `feat(authz): rbac capability matrix seeded as data`

---

#### T-028 — The can(user, action, obj) entry point

**References** — T-027 grants; T-026 assignment scoping. `limited`/`own` levels resolve against the object, which is why this is one function and not a decorator soup.

**Do** — `apps/authz/services.py::can(user, action, obj=None) -> bool`. Resolution order: active `Membership` role → `RoleGrant` level → object-level refinement (`own` compares actor identity; `limited` consults assignment). Provide a `require_can` view decorator and an `{% if can %}` template helper. **No view may check `role ==` directly** — add a guard test grepping for role-equality checks outside `apps/authz/`.

> **Retrofit T-017 in THIS commit.** T-017 was explicitly instructed to gate invite issuance on `Membership.role` "until `can()` is available". That interim code is exactly what this todo's guard test is built to reject, so without the retrofit **this commit is red by construction** and violates "every commit leaves the tree green". Replace the interim gate in the invite view with `require_can("users.create")` here, in the same commit.

**Acceptance criteria** — Unit tests cover every level for at least one capability each. A staff accountant is denied on an unassigned client but allowed on an assigned one. The grep guard passes. `can()` fails closed on an unknown capability slug (raises, not returns True).
**QA — happy**: `uv run pytest tests/authz/ -v` → `.evidence/T-028-happy.txt`
**QA — failure**: call `can(user, "nonexistent.capability")` and assert it raises; add a stray `role == "owner"` check in a view and confirm the guard test fails → `.evidence/T-028-failure.txt`
**Commit**: `feat(authz): single can() entry point with object-level refinement`

---

#### T-029 — Onboarding checklist

**References** — Anchor §5 "onboarding checklist with gov.br/certificate readiness score". Report journey (lines 100): the checklist explains whether the client needs a silver/gold gov.br account, a certificate, or a manual portal step.

**Do** — `OnboardingItem(TenantScopedModel)`: FK client, `key`, `label`, `status` (`pending`/`blocked`/`done`/`not_applicable`), `blocked_reason`, `completed_at`. A template of default items seeded per new client: tax profile complete, gov.br trust level recorded, certificate status recorded, first DAS schedule known, annual declaration status known, invoice model configured. Items are data-driven so Phase 3 can add connector items without migration.

**Acceptance criteria** — Creating a `ClientCompany` auto-creates the default checklist. Item set is data-driven (adding a template row adds it to new clients). Completing all items flips a derived `is_ready` property.
**QA — happy**: create client → assert default items exist → `.evidence/T-029-happy.txt`
**QA — failure**: mark one item `blocked`; assert `is_ready` is False and the blocked reason is required → `.evidence/T-029-failure.txt`
**Commit**: `feat(clients): data-driven onboarding checklist`

---

#### T-030 — Readiness score and gov.br trust level

**References** — Anchor §4: "the onboarding checklist still *records* each client's gov.br trust level (bronze/prata/ouro) as data" — recording only; **no gov.br OIDC integration in this scope**. Report lines 29: e-CAC requires prata or ouro.

**Do** — Add `govbr_trust_level` (`bronze`/`prata`/`ouro`/`unknown`) and `has_digital_certificate` (bool) + `certificate_expires_on` (date, **metadata only** — anchor §7 forbids certificate custody in v1) to `ClientCompany`. A `readiness_score` computed property: percentage of non-`not_applicable` checklist items done, with a hard `blocked` flag if trust level is `bronze`/`unknown` while an item requires e-CAC.

**Acceptance criteria** — Score is 0 for a fresh client, 100 when all applicable items are done. A `bronze` trust level surfaces the e-CAC blocker. No certificate **file** field exists anywhere (assert by model introspection — only metadata).
**QA — happy**: score progression test 0→100 → `.evidence/T-030-happy.txt`
**QA — failure**: assert no `FileField`/`BinaryField` exists on `ClientCompany` for certificates → `.evidence/T-030-failure.txt`
**Commit**: `feat(clients): readiness score with govbr trust level metadata`

---

### Wave 6 — Fiscal primitives

#### T-031 — VERIFY-FIRST: extract the CNPJ alfanumérico check-digit algorithm

**References** — **Time-critical**: IN RFB nº 2.229/2024, in force since 2024-10-25; alphanumeric CNPJs enter production for new registrations **2026-07-31**. Anchor §6 describes the rule as "weights over ASCII value − 48" with módulo 11. Research confirmed 14 positions, `0-9` + `A-Z`, módulo 11 — but **could not extract the exact weight sequence** (blocked parsing the RFB PDF). Do not guess.

**Do** — Confirm against the Receita Federal CNPJ alfanumérico page and its Q&A PDF (`https://www.gov.br/receitafederal/pt-br/.../cnpj-alfanumerico`). Independent review has already established the following from those sources — **verify, do not rediscover**:
- Layout is **12 alphanumeric positions + 2 NUMERIC check digits**. Check digits are never letters.
- Character value = `ord(char) - 48` (so `'0'`→0 … `'9'`→9, `'A'`→17 … `'Z'`→42).
- Módulo 11 with weights cycling **2..9** applied right-to-left; remainder `< 2` → digit `0`, else `11 - remainder`.
- Legacy all-numeric CNPJs validate **identically** under the same algorithm — it is backward compatible, which is why both formats coexist.
- Official worked example: **`12.ABC.345/01DE-35`**.

> **RFB publishes exactly ONE official worked example.** An acceptance criterion demanding "≥3 official vectors" is unsatisfiable and would trigger a false STOP that blocks all of Wave 6. Require the one official vector, and generate the rest as self-computed vectors clearly labelled as derived.

**Acceptance criteria** — `.evidence/T-031-verify.txt` records the weight vectors, the ASCII−48 rule, the remainder rule, and **the single official RFB vector `12.ABC.345/01DE-35` with its source URL**, plus an explicit confirmation that check digits are numeric-only and that legacy numeric CNPJs validate under the same algorithm. If RFB's published algorithm differs from the above in any particular, STOP and report.
**QA — happy**: primary-source citation + test vectors captured → `.evidence/T-031-happy.txt`
**QA — failure**: if only secondary sources are reachable, record the STOP report naming what is missing → `.evidence/T-031-failure.txt`
**Commit**: `docs(fiscal): capture cnpj alfanumerico algorithm from primary source`

---

#### T-032 — CNPJ and CPF validators, property-tested

**References** — T-031's verified algorithm (**do not start before it lands**). Anchor §6: "implement from the published spec (~30 lines) and property-test it". Anchor §9 mandates the CNPJ regression pack.

**Do** — `apps/fiscal/validators.py`: `validate_cnpj(value)` and `validate_cpf(value)`, both operating on **normalized uppercase alphanumeric** input. Normalizer strips mask characters and uppercases. Reject wrong length, invalid characters, and bad check digits with distinct messages. Property tests with `hypothesis`: every generated valid CNPJ validates; mutating any single character invalidates it; all legacy numeric CNPJs from the test corpus still validate.

> **⚠ THE OBVIOUS PROPERTY TEST IS MATHEMATICALLY FALSE — do not write it.** "Mutating any single character invalidates the CNPJ" is **not true** under módulo 11 over an alphabet whose values span more than 11. Character values are `ord(c) − 48`, so `'A'`=17, `'L'`=28, `'W'`=39 — all ≡ 6 (mod 11). Substituting one for another changes the weighted sum by a multiple of 11 and leaves both check digits **unchanged**. Concretely, `12.LBC.345/01DE-35` is a *valid* CNPJ derived from the official `12.ABC.345/01DE-35`. Hypothesis will find this counterexample within a few hundred examples and the test will fail — correctly.
>
> Write the **true** property instead: *mutating a character to one with a **different** residue mod 11 invalidates the checksum.* Add an explicit regression test documenting the collision classes as a known, intended property of the official algorithm — a future maintainer must not "fix" it. **The classes span digits AND letters, so a substitution generator restricted to A–Z would miss half of them.**

> **DO NOT TRANSCRIBE A RESIDUE TABLE FROM THIS DOCUMENT.** Three separate review rounds each shipped a *wrong* hand-written table here (the last one asserted `{0,E,P} ≡ 0`; in fact `'E'`=21 and `'P'`=32 are both ≡ **10**, and ≡0 is `{0,F,Q}`). A hand-copied table is exactly the artefact that keeps being wrong. **Build it in code** — `collections.defaultdict(list)` over `"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"` keyed on `(ord(c) - 48) % 11` — and assert the generated mapping's *properties*, not a literal copy: every class is non-empty, classes partition the full 36-character alphabet, and same-class substitution within positions 1–12 leaves both check digits unchanged.

The one collision that IS verified and must be asserted as a concrete regression case: **`12.LBC.345/01DE-35` is VALID**, derived from the official `12.ABC.345/01DE-35` by the A→L substitution (both ≡ 6 mod 11).

- Also reject the classic degenerate inputs that pass the arithmetic: all-identical characters (`00000000000000`, `11111111111111`, …) for CNPJ, and the same for CPF.

**Acceptance criteria** — The **single official RFB vector** `12.ABC.345/01DE-35` passes, plus ≥5 self-computed vectors clearly labelled as derived. Legacy numeric CNPJs validate. The **residue-aware** mutation property holds over ≥200 Hypothesis examples. `12.LBC.345/01DE-35` is asserted **valid** (the documented collision). `validate_cnpj("00000000000000")` is rejected by the degenerate-input rule, not by arithmetic. Lowercase input is normalized, not rejected.
**QA — happy**: `uv run pytest tests/fiscal/test_validators.py -v` → `.evidence/T-032-happy.txt`
**QA — failure**: feed a CNPJ with a deliberately corrupted final digit and a 13-character string; assert distinct error messages → `.evidence/T-032-failure.txt`
**Commit**: `feat(fiscal): alphanumeric-ready cnpj and cpf validators with property tests`

---

#### T-033 — Alphanumeric-ready storage, masks, search, and export

**References** — Anchor §6: "Masks, search inputs, exports, and connector payloads must all be alphanumeric-ready." Report line 31 treats this as a hard engineering requirement across "every validator, mask, search input, API adapter, XML serializer, and database field".

**Do** — Wire the T-032 validators onto `ClientCompany.cnpj`/`cpf`. Store normalized (no punctuation, uppercase). Add a `format_cnpj()` display helper producing `AA.AAA.AAA/AAAA-DD`. Make admin and app search normalize the query before matching, so a user pasting a masked CNPJ finds the record. Add a CSV export that emits the **normalized** value in a text-safe way (leading-zero and letter safe — never coerced to a number).

**Acceptance criteria** — Searching by masked input finds a record stored normalized. Export round-trips an alphanumeric CNPJ byte-for-byte. A CNPJ with leading zeros is never truncated. Display helper output matches the official mask.
**QA — happy**: search-by-mask + export round-trip → `.evidence/T-033-happy.txt`
**QA — failure**: assert an alphanumeric CNPJ is **not** mangled by export (no scientific notation, no leading-zero loss) → `.evidence/T-033-failure.txt`
**Commit**: `feat(fiscal): normalized alphanumeric cnpj storage, masks, search, export`

---

#### T-034 — CNPJ alfanumérico regression pack

**References** — Anchor §9: this is one of the two mandated day-one packs.

**Do** — `tests/regression/test_cnpj_alfanumerico.py` asserting an alphanumeric CNPJ survives, in one end-to-end pass: validation → normalization → model save → queryset filter → admin search → CSV export → re-import. Include legacy numeric CNPJs in the same pack to prove coexistence (both formats are valid simultaneously per IN RFB 2.229/2024). CI runs this pack as its own named step so a regression is unmistakable.

**Acceptance criteria** — Pack passes for both alphanumeric and legacy numeric fixtures. CI shows it as a distinct step. Removing the normalizer from any one layer turns the pack red.
**QA — happy**: `uv run pytest tests/regression/test_cnpj_alfanumerico.py -v` → `.evidence/T-034-happy.txt`
**QA — failure**: bypass normalization in the search layer only; confirm the pack fails there specifically; revert → `.evidence/T-034-failure.txt`
**Commit**: `test(regression): cnpj alfanumerico end-to-end pack`

---

#### T-035 — Municipality capability registry (shape only)

**References** — Anchor §4: "Seed a municipality capability registry from day one, even as mostly-static data." Anchor §9 mandates the municipality-variance pack. **Scope limit**: data shape and lookup only — **no NFS-e API calls**, which are Phase 3.

**Do** — `Municipality` (IBGE code PK, name, UF) and `MunicipalityCapability` (FK municipality, `nfse_national_emitter` bool, `requires_certificate` bool, `notes`, `verified_on`). These are **platform-level, not tenant-scoped** — add them to `NON_TENANT_TABLES` (T-010). Seed the capital cities plus any municipality referenced by seeded test clients. A `capability_for(ibge_code)` lookup returning a safe unknown default rather than raising.

**Acceptance criteria** — Lookup returns a record for a seeded municipality and an explicit `unknown` for an unseeded one (never an exception, never a silent True). Tables appear in `NON_TENANT_TABLES` and the T-014 coverage meta-test still passes. `tests/regression/test_municipality_registry.py` locks the field shape.
**QA — happy**: seeded + unseeded lookups → `.evidence/T-035-happy.txt`
**QA — failure**: assert an unseeded code yields `unknown` with `requires_certificate` defaulting to the **conservative** value, not permissive → `.evidence/T-035-failure.txt`
**Commit**: `feat(fiscal): municipality capability registry with variance pack`

---

### Wave 7 — Obligation engine and DAS calendar

#### T-036 — Versioned, effective-dated parameter tables

**References** — Anchor §6: "The obligation engine is data, not code. Obligation types, due-date rules, and MEI parameters live in versioned parameter tables. Limits and dates change politically; the engine shouldn't need a deploy when they do." **Strongly validated by research**: the R$81.000 ceiling is confirmed for 2026, but increases to R$110.000 (2027) and R$140.000 (2028) are actively before the Chamber and **not yet enacted**. A hardcoded ceiling is a guaranteed future outage.

**Do** — `FiscalParameter` (platform-level, not tenant-scoped): `key`, **`mei_category`** (`common` / `caminhoneiro`, nullable for category-agnostic keys), `value` (string, cast at read), `valid_from`, `valid_to` (nullable), `source_note`, `source_url`.

> **The ceiling is NOT a single scalar.** MEI-Caminhoneiro carries its own substantially higher annual ceiling (≈ R$ 251.600) alongside common MEI's R$ 81.000. Modelling `mei.annual_ceiling` as one global value silently measures every trucker client against the wrong limit. Add `mei_category` to `ClientCompany` (default `common`) and make the resolver category-aware from day one, even if only common-MEI rows are seeded initially. `ObligationType`: `code` (`DAS`, `DASN`), `name`, `periodicity`, `due_rule` (structured JSON, not code). A `parameter_for(key, on_date, mei_category=None)` resolver returning the row effective on that date, raising `NoEffectiveParameter` loudly if none covers it.

> **DO NOT use an `EXCLUDE`/`ExclusionConstraint` for overlap rejection.** It looks right and is a trap here. With `valid_to = NULL` meaning "open-ended", the 2026 row spans `[2026-01-01, ∞)`; inserting a 2027 row spanning `[2027-01-01, ∞)` **overlaps it**, so the constraint would reject the exact insert that T-036's, T-037's, and T-042's acceptance criteria all require to succeed — destroying the plan's headline "change the 2027 ceiling with no deploy" property. It also silently does nothing for the three rows whose `mei_category` is `NULL`, because `NULL = NULL` is not true in a GiST equality operator. And it drags in `django.contrib.postgres`, a `btree_gist` extension, and a `CREATE`-on-database grant `app_migrator` does not have.
>
> **Use latest-wins effective dating instead.** `parameter_for(key, on_date, mei_category)` selects the row with the **greatest `valid_from` that is ≤ `on_date`**, filtered to `valid_to IS NULL OR valid_to > on_date`. Superseding a value is then a pure insert — exactly the property the plan promises — and no extension, constraint, or close-then-insert dance is needed. Add a data-quality test asserting no two rows share an identical `(key, mei_category, valid_from)` via a plain `UniqueConstraint`, which is all the integrity this pattern actually requires.

**Acceptance criteria** — `parameter_for("mei.annual_ceiling", date(2026,6,1))` returns `81000.00`. Inserting a 2027 row with `valid_from=2027-01-01` changes the 2027 answer **without any code change** — proven by a test. Requesting a date with no covering row raises `NoEffectiveParameter`, never returns a default. Two rows sharing an identical `(key, mei_category, valid_from)` are rejected by `UniqueConstraint(fields=["key","mei_category","valid_from"], nulls_distinct=False)` — **`nulls_distinct=False` is required** (Django 5.0+/PG15+), because PostgreSQL treats NULLs as distinct by default and three seeded rows carry `mei_category = NULL`, so a re-run would silently duplicate them and make latest-wins ambiguous. **Overlapping validity ranges are PERMITTED** and resolved latest-wins — the open-ended 2026 row necessarily overlaps any 2027 row, and permitting that insert *is* the no-deploy property.
**QA — happy**: resolver tests across 2026 and a hypothetical 2027 row → `.evidence/T-036-happy.txt`
**QA — failure**: insert a duplicate `(key, mei_category, valid_from)` → assert `IntegrityError`; then insert an open-ended 2027 row over the open-ended 2026 row → assert it **succeeds**, `parameter_for(..., 2027-06-01)` returns the new value and `parameter_for(..., 2026-06-01)` still returns `81000.00`; then query an uncovered date → assert `NoEffectiveParameter` → `.evidence/T-036-failure.txt`
**Commit**: `feat(obligations): effective-dated fiscal parameter tables`

---

#### T-037 — Seed 2026 MEI parameters with sources

**References** — Verified 2026-07-27: MEI annual ceiling **R$ 81.000** (unchanged for 2026); proportional opening-year rule **R$ 6.750 × months active**; DAS due **day 20** of the following month; MEI employee limit **1**. Increases for 2027–2028 are proposed, **not enacted** — do not seed them.

**Do** — Data migration seeding, each with `valid_from = 2026-01-01`, `valid_to = NULL`, and a `source_url` pointing at the official gov.br page:

| key | `mei_category` | value |
|---|---|---|
| `mei.annual_ceiling` | `common` | `81000.00` |
| `mei.annual_ceiling` | `caminhoneiro` | `251600.00` |
| `mei.monthly_proportional` | `common` | `6750.00` |
| `mei.monthly_proportional` | `caminhoneiro` | `20966.67` |
| `mei.excess_tolerance_pct` | `NULL` | `0.20` |
| `das.due_day` | `NULL` | `20` |
| `mei.max_employees` | `NULL` | `1` |

> **Two gaps this closes.** (a) T-042 requires the caminhoneiro ceiling and the tolerance rate, and T-036's resolver **raises `NoEffectiveParameter`** when a key is unseeded — so omitting them made T-042's acceptance criteria unreachable. (b) The **proportional monthly rate is per-category too**, not just the annual ceiling: a caminhoneiro in its opening year must be measured against R$ 20.966,67/month, not R$ 6.750. Measuring it against the common rate would flag a compliant client as over-limit.

- Every row must state `mei_category` explicitly (`NULL` meaning category-agnostic) — leaving it unstated forces the executor to guess, and the two guesses give different resolver behaviour.
- The caminhoneiro figures derive from LC 188/2021; cite it in `source_note` rather than carrying an approximation.
- Add `docs/regulatory-watch.md` noting the pending ceiling legislation and the monthly review ritual (anchor §9).

**Acceptance criteria** — All seven parameters resolve for any 2026 date with the documented values and categories. Every seeded row has a non-empty `source_url` and an explicit `mei_category` (possibly `NULL`). Resolving `mei.annual_ceiling` for a `caminhoneiro` client returns `251600.00`, for a `common` client `81000.00`. No 2027/2028 ceiling row exists (seeding unenacted law would be a correctness bug). `docs/regulatory-watch.md` names the pending bills.
**QA — happy**: resolve all **seven** for 2026-07-27, asserting the category-aware split for both `mei.annual_ceiling` and `mei.monthly_proportional` → `.evidence/T-037-happy.txt`
**QA — failure**: assert `parameter_for("mei.annual_ceiling", date(2027,1,1))` still returns 81000 (the open-ended 2026 row) and that no enacted-2027 row was seeded → `.evidence/T-037-failure.txt`
**Commit**: `feat(obligations): seed 2026 mei parameters with official sources`

---

#### T-038 — Encode the DASN-SIMEI deadline rule

**References** — **RESOLVED — do not re-research.** DASN-SIMEI is due **31 May with no rolling whatsoever**: the deadline does not move even when 31 May falls on a Saturday or Sunday. Basis: Resolução CGSN nº 140/2018 art. 109, confirmed by RFB's official MEI Q&A and by 2025, when the deadline fell on Saturday 31 May and did not shift. **The anchor document's "last business day of May" (§4, §8) is incorrect** — this supersession is recorded in the Document precedence table.

**Do** — Seed the `DASN` `ObligationType` with `periodicity='annual'` and `due_rule = {"month": 5, "day": 31, "direction": "none"}`. The `none` direction must be a first-class value in T-040's resolver, not a special case. Record the CGSN citation in the row's `source_note`/`source_url`.

**Acceptance criteria** — DASN for competence year 2026 resolves to **2027-05-31**. A year where 31 May falls on a Sunday still resolves to 31 May (assert with a year where this holds, e.g. 2026-05-31 was a Sunday). The rule row carries a non-empty `source_url`. No code path special-cases DASN — behaviour comes from the `direction: none` data value.
**QA — happy**: resolve DASN for two years, one with a weekend 31 May → `.evidence/T-038-happy.txt`
**QA — failure**: temporarily flip the seeded direction to `forward`, assert the weekend-year test FAILS (proving the test detects incorrect rolling), then revert → `.evidence/T-038-failure.txt`
**Commit**: `feat(obligations): encode dasn-simei fixed 31 may deadline`

---

#### T-039 — National holiday table

**References** — Anchor §6: "a national holiday table (municipal holidays later)". Anchor §8: due-date logic rolls "past weekends/holidays".

**Do** — `Holiday` (platform-level): `date`, `name`, `scope` (`national` for now; the column exists so municipal holidays need no migration later). Seed Brazilian **national** holidays for 2026 and 2027, including movable feasts (Carnaval, Sexta-feira Santa, Corpus Christi) computed from Easter, not hardcoded — a data migration may materialize them, but the computation must be tested. Add to `NON_TENANT_TABLES`.

**Acceptance criteria** — 2026 and 2027 national holidays are present. The Easter-derived dates are correct for both years (assert against known values). A `is_business_day(date)` helper returns False for Saturdays, Sundays, and seeded holidays.
**QA — happy**: assert known 2026 holiday dates incl. movable feasts → `.evidence/T-039-happy.txt`
**QA — failure**: assert `is_business_day` is False on a seeded holiday that falls midweek → `.evidence/T-039-failure.txt`
**Commit**: `feat(obligations): national holiday table with movable feast computation`

---

#### T-040 — Business-day rolling logic

**References** — **RESOLVED, encode as given** (see "Resolved fiscal rules" above). DAS-MEI rolls **FORWARD** to the next business day per Resolução CGSN nº 140/2018 art. 40 §3. DASN-SIMEI does **not roll at all** (art. 109). DAE-MEI, if it ever enters scope, rolls **BACKWARD** — which is exactly why direction is data, not code.

**Do** — `apps/obligations/calendar.py::resolve_due_date(nominal_date, rule)` where `rule` specifies `direction ∈ {forward, backward, none}` and the calendar to consult. Handle chains: a holiday adjacent to a weekend must skip the entire run, not one day.

**Acceptance criteria** — A nominal day-20 falling on a Saturday resolves **forward** to the following Monday (or later if Monday is a holiday). A three-day holiday-plus-weekend run resolves past the entire block. `direction='none'` returns the nominal date unchanged even on a Sunday — the DASN case. Direction is read from the `ObligationType.due_rule` row, so changing it in data changes the result with no code edit.
**QA — happy**: rolling tests over weekend, single holiday, and holiday+weekend chains → `.evidence/T-040-happy.txt`
**QA — failure**: flip the rule's direction in data and assert the resolved date changes accordingly → `.evidence/T-040-failure.txt`
**Commit**: `feat(obligations): data-driven business-day rolling with verified direction`

---

#### T-041 — Twelve-month DAS calendar generator

**References** — Anchor §12 exit criterion: "a firm tenant with two users and three fake clients sees a correct 12-month DAS calendar in staging". Anchor §8: the DAS loop must be excellent because day 20 is immovable.

**Do** — `Obligation(TenantScopedModel)`: FK client, FK obligation_type, `competence_month`, `nominal_due_date`, `resolved_due_date`, `status` (`scheduled`/`due`/`paid`/`overdue`/`waived`), `evidence_ref` (nullable — the upload flow itself is Phase 2). A generator producing 12 months of DAS obligations for a client, honouring T-036 parameters and T-040 rolling.

- **Idempotency must be a database constraint, not a convention**: `UniqueConstraint(fields=["tenant","client","obligation_type","competence_month"])`. `task_acks_late = True` (T-006) means a task can legitimately execute twice after a worker loss, so duplicate obligations are an expected input, not a hypothetical.
- **Handle the constraint violation without poisoning the transaction.** A raw `IntegrityError` inside the task's atomic block aborts the *whole* transaction, so a re-run would fail loudly instead of being a no-op — defeating the point. Use `bulk_create(..., ignore_conflicts=True)`, or wrap each `get_or_create` in its own `transaction.atomic()` savepoint.
- A Celery beat task regenerates the rolling window, wrapped in `tenant_context` per T-013.
- **Beat dead-man's switch — with concrete numbers.** This product's core promise is "never miss day 20". If the `beat` container is simply *down*, nothing raises, nothing is generated, and Sentry sees nothing, because nothing failed. Add `SchedulerHeartbeat` (**platform-level — add to `NON_TENANT_TABLES` in this commit**), updated by a beat task running **every 5 minutes**. `/healthz` returns **503** when `max(SchedulerHeartbeat.updated_at)` is older than **15 minutes**. Silence must be detectable.

**Acceptance criteria** — Generating for a client yields exactly 12 `Obligation` rows with correct competence months and resolved due dates. Re-running is a no-op (unique constraint on client+type+competence) **and raises no `IntegrityError`**. Generation for a client whose `opened_on` is mid-year starts at the correct month, not January. The beat task runs under explicit tenant context. **Dead-man's switch: with the newest `SchedulerHeartbeat` artificially aged past 15 minutes, `/healthz` returns 503; aged 5 minutes, it returns 200.** `obligations_schedulerheartbeat` is present in `NON_TENANT_TABLES` and the T-014 coverage meta-test still passes.
**QA — happy**: generate for three clients, assert 12 rows each with correct dates → `.evidence/T-041-happy.txt`
**QA — failure**: run the generator twice; assert row count is still 12, not 24 → `.evidence/T-041-failure.txt`
**Commit**: `feat(obligations): idempotent twelve-month das calendar generator`

---

#### T-042 — MEI revenue threshold monitor

**References** — Anchor §5: "a **monthly revenue vs. R$ 81.000 threshold monitor** (proportional in the opening year, ≈ R$ 6.750 × months) … the highest-value simple feature in the whole bookkeeping area." Ceiling and proportional rate come from T-036 parameters, never constants. **Scope limit**: revenue is entered manually or seeded in this phase — invoice and bank ingestion are Phases 3–4.

**Do** — `MonthlyRevenue(TenantScopedModel)`: FK client, `competence_month`, `gross_amount` `DECIMAL(14,2)`, with **`UniqueConstraint(fields=["tenant","client","competence_month"])`** — without it a duplicate entry double-counts revenue and can push a compliant client over the ceiling. All arithmetic in `Decimal`, never float. A `threshold_status(client, year)` service returning accumulated revenue, the ceiling applicable **to that client's MEI category** (T-036), percentage consumed, and a band.

> **THE 20% TOLERANCE BAND IS NOT COSMETIC — it changes the legal outcome, and a monitor that omits it actively misinforms the accountant.** Brazilian rules treat two distinct overage cases very differently:
> - **Excess ≤ 20%** of the ceiling — the client remains MEI through 31 December and is desenquadrado effective **1 January of the following year**, settling the difference retroactively.
> - **Excess > 20%** — desenquadramento is retroactive to **1 January of the current year**, except in the **opening year**, where it is retroactive to the client's **`opened_on`** date; the whole period is recomputed as ME.
>
> That is **four** legislated outcomes, not two: {within tolerance, over tolerance} × {opening year, subsequent year}. All four must be asserted — testing only the two subsequent-year cases leaves the opening-year >20% branch, the one resolving to `opened_on` rather than 1 January, completely unexercised.
>
> Reporting only "over the limit" gives an identical signal for two situations a full year apart in consequence.

- Bands: `ok` (<80%), `warning` (80–100%), `exceeded_within_tolerance` (100% to +20%), `exceeded_over_tolerance` (>+20%). Each exceeded band must report the **computed desenquadramento effective date**.
- The 20% tolerance rate is itself a `FiscalParameter` row, never a constant.
- **RESOLVED — not a verify-first gate.** The 20% threshold and both effective-date rules are confirmed against **Resolução CGSN nº 140/2018 art. 115**; cite it in the seeded row's `source_note`. (This was previously worded as a stop-and-report gate, which was incoherent: T-037 seeds `mei.excess_tolerance_pct = 0.20` earlier in the same wave, so the gate would have fired after the value it governs was already committed.)
- **The proportional rule counts a partial month as a WHOLE month** (LC 123/2006 art. 18-A §2). A client opening on 20 July is measured over six months (July–December), not 5.4. State this explicitly — computing fractional months is the obvious wrong implementation.

**Acceptance criteria** — A client opened in January is measured against its category ceiling. A client opened in July of its first year is measured against the proportional monthly rate × 6. Bands trigger at exactly 80%, 100%, and +20% boundaries. All **four** legislated outcomes are asserted, not two — an implementation returning `date(year, 1, 1)` for every over-tolerance case must go red:
  1. Subsequent year, 110% → `exceeded_within_tolerance`, effective **1 January next year**.
  2. Subsequent year, 125% → `exceeded_over_tolerance`, effective **1 January current year**.
  3. **Opening year** (`opened_on = 2026-07-20`), 110% of the proportional ceiling → `exceeded_within_tolerance`, effective **1 January next year**.
  4. **Opening year**, 125% of the proportional ceiling → `exceeded_over_tolerance`, effective **`opened_on` (2026-07-20)** — *not* 1 January. This is the branch the callout exists to protect. A MEI-Caminhoneiro client is measured against the caminhoneiro ceiling, not the common one. The ceiling is read from `FiscalParameter`, proven by inserting a hypothetical 2027 row and observing the status change with no code edit. No float appears in the computation path.
**QA — happy**: full-year and proportional cases plus band boundaries → `.evidence/T-042-happy.txt`
**QA — failure**: assert a `float` never enters the calculation (type assertion) and that exceeding the proportional ceiling flags `exceeded` even when below 81000 → `.evidence/T-042-failure.txt`
**Commit**: `feat(obligations): mei revenue threshold monitor with proportional opening year`

---

### Wave 8 — Work queues and accountant dashboard

#### T-043 — Frontend asset pipeline: Tailwind, HTMX, Alpine

**References** — Anchor §2: "Django templates + HTMX + Alpine.js + Tailwind. One codebase; server-rendered; SPA-feel where it matters (queues, dashboards)." Tailwind v4 uses CSS-first config (`@import "tailwindcss"`), not `tailwind.config.js` — verify the installed major before writing config.

**Do** — Add a Tailwind build step producing a hashed stylesheet into `static/`, served by whitenoise. Vendor HTMX and Alpine as pinned local assets (no CDN — a fiscal product should not depend on third-party availability, and CSP is tighter without it). Base template loading them with `defer`. Add a CSP header allowing no external script origins.

**Acceptance criteria** — `manage.py collectstatic` produces the hashed CSS and JS. A rendered page includes HTMX and Alpine from same-origin paths. `git grep` finds no `cdn.` script src. CSP header present in prod settings and does not include `unsafe-inline` for scripts.
**QA — happy**: collectstatic + render a page + dump response headers → `.evidence/T-043-happy.txt`
**QA — failure**: assert no external script origin appears in any template → `.evidence/T-043-failure.txt`
**Commit**: `feat(ui): tailwind, htmx, and alpine asset pipeline with strict csp`

---

#### T-044 — Base layout and pt-BR formatting

**References** — Anchor §2 (pt-BR localization, BRT scheduling), report NFR table (WCAG 2.2 AA baseline). Anchor §6: dates stored UTC, displayed in `America/Sao_Paulo`.

**Do** — `templates/base.html` with a landmark structure (`header`/`nav`/`main`/`footer`), skip link, visible focus styles, and `lang="pt-br"`. Template filters for BRL currency (`R$ 1.234,56`) and pt-BR dates. A tenant-aware nav showing only capabilities the user passes `can()` for.

**Acceptance criteria** — Currency filter renders `1234.5` as `R$ 1.234,50`. A UTC-stored timestamp renders in BRT. Every page has exactly one `<h1>` and a working skip link. Nav hides items the user cannot access (asserted for a staff accountant vs an owner).
**QA — happy**: filter tests + landmark/skip-link assertions → `.evidence/T-044-happy.txt`
**QA — failure**: assert a staff accountant's nav omits owner-only items → `.evidence/T-044-failure.txt`
**Commit**: `feat(ui): accessible pt-BR base layout with capability-aware nav`

---

#### T-045 — Work queue queries

**References** — Anchor §8 Phase 1 "work queues"; report journey (line 104): the dashboard shows which clients have missing items, which have DAS due, and which have exceptions.

**Do** — `apps/obligations/queries.py` returning, all tenant-scoped and assignment-aware via `can()`: **Due soon** (obligations resolving in the next 7 days), **Overdue**, **Onboarding blocked** (checklist items `blocked`), **Threshold warning** (clients in `warning`/`exceeded` bands). Each returns a stable, paginated queryset with `select_related` to avoid N+1.

**Acceptance criteria** — Each queue returns only the requesting tenant's rows. A staff accountant sees only assigned clients; an owner sees all. Queries execute within a fixed query-count budget asserted by `assertNumQueries` (proving no N+1). Empty states return empty querysets, never None.
**QA — happy**: queue content + `assertNumQueries` budgets → `.evidence/T-045-happy.txt`
**QA — failure**: seed 50 clients and assert the query count does **not** scale with row count → `.evidence/T-045-failure.txt`
**Commit**: `feat(obligations): assignment-aware work queue queries`

---

#### T-046 — Work queue views with HTMX

**References** — T-045 queries, T-043/T-044 UI, T-028 `can()` gating.

**Do** — Views and templates for the four queues with HTMX partial refresh, server-side pagination, and filtering by assignee and status. Every view guarded by `require_can`. Queue counts update without a full page load.

**Acceptance criteria** — Each queue renders for an owner and a staff accountant with correctly different row sets. An unauthorized capability returns 403, not an empty list (a silent empty list would hide a permissions bug). HTMX partial requests return fragments, full requests return whole pages. Pagination is stable across requests.
**QA — happy**: render all four queues for both roles → `.evidence/T-046-happy.txt`
**QA — failure**: request a queue as a user lacking the capability; assert **403**, not 200-with-zero-rows → `.evidence/T-046-failure.txt`
**Commit**: `feat(obligations): htmx work queue views with capability guards`

---

#### T-047 — Accountant dashboard v1

**References** — Anchor §8 Phase 1 "accountant dashboard v1". Report line 37: the firm owner persona values dashboards, queue views, and portfolio visibility. This todo delivers the plan's headline exit criterion.

**Do** — A dashboard at the tenant root showing: portfolio counts by client status, the four queue counts from T-045 with links, a 12-month DAS calendar strip for a selected client (the anchor's §12 exit artifact), and threshold-warning clients. Owner sees the whole portfolio; staff accountant sees their assignments.

**Acceptance criteria** — The dashboard renders for a tenant with 2 users and 3 client companies and displays a **correct 12-month DAS calendar** with properly rolled due dates — the anchor §12 exit criterion, asserted end-to-end through the HTTP layer. Counts match the underlying queries exactly. A second tenant's data never appears. Query-count budget asserted.
**QA — happy**: full exit-criterion assertion — 2 users, 3 clients, correct 12-month calendar over HTTP → `.evidence/T-047-happy.txt`
**QA — failure**: authenticate as tenant B and assert none of tenant A's clients or obligations appear anywhere in the rendered dashboard → `.evidence/T-047-failure.txt`
**Commit**: `feat(reports): accountant dashboard v1 with portfolio and das calendar`

<!-- END TODOS -->

---

## Final verification wave

Runs after ALL todos. All four must APPROVE. Run in parallel; surface results and wait for explicit user sign-off before declaring the plan complete.

**F1 — Plan compliance audit.** Verify all 50 todos are implemented (T-001–T-047, plus T-002a; T-007 replaced by T-007a/T-007b; T-019 replaced by T-019a/T-019b/T-019c; no T-015), each acceptance criterion passes when re-run now, and each `.evidence/` file exists and is non-empty. Fail on any todo whose evidence is missing or stale.

**F2 — Code quality review.** `ruff check`, `ruff format --check`, `mypy .` all clean. No `# type: ignore` without a justifying comment. No `.all_tenants()` call outside an explicitly documented allow-list. No secret literal in the repo (`git grep` for key patterns).

**F3 — Real manual QA, agent-executed.** From a cold `docker compose down -v && docker compose up -d --wait`: run migrations, seed two firm tenants with two users and three client companies each, and assert end-to-end that (a) each firm sees only its own clients through the HTTP layer, (b) a correct 12-month DAS calendar renders, (c) an alphanumeric CNPJ round-trips through create → search → export. Evidence: `.evidence/F3-cold-boot.txt`.

**F4 — Scope fidelity.** Confirm nothing from the Must-NOT-Have table was built. Specifically assert absence of: PSP/Asaas code, invoice models, document-upload views, push/service-worker assets, bank-import parsers, connector implementations. Fail on any hit.

---

## Commit strategy

- **One commit per todo.** No todo spans commits; no commit spans todos.
- Conventional Commits, matching the developer's existing style (`feat(scope): …`, `fix(db): …`, `chore(config): …`, `test(scope): …`).
- Every commit must leave the tree green: lint, types, and tests all pass. Never commit a red suite.
- Migrations are committed **with** the code that requires them.
- The commit body names the todo id, e.g. `Implements T-012.`
- Branch: trunk-based on `main`. This is a solo build; no PR ceremony, but CI must be green on every push.

---

## Success criteria

This plan is complete when **all** of the following hold:

1. `docker compose up -d --wait` from a clean clone brings up web, worker, beat, Postgres, and Redis with no manual steps beyond copying `.env.example` to `.env`.
2. CI is green on `main`: ruff, mypy, pytest, missing-migrations check, and the isolation suite.
3. **Cross-tenant reads fail.** `tests/isolation/` proves, under the non-superuser app role via `TransactionTestCase`, that tenant A cannot read tenant B's rows, and that a query with **no** tenant context returns **zero** rows.
4. **No tenant-scoped table lacks a policy.** The meta-test over `pg_class.rowsecurity` and `pg_policies` passes, and fails if a new tenant-scoped model is added without RLS.
5. Alphanumeric CNPJs pass end-to-end through validation, normalization, storage, admin search, filtering, and CSV export — proven by the CNPJ regression pack.
6. A firm tenant with two users and three client companies renders a correct 12-month DAS calendar in staging, with day-20 due dates rolled correctly past weekends and national holidays.
7. The MEI ceiling, DAS day, DASN deadline, and one-employee flag are **rows in versioned, effective-dated parameter tables** — not constants in code. Changing the 2027 ceiling requires no deploy.
8. MFA is enforced for all firm-side roles; `audit.Event` is append-only at the database grant level; the access-log stream is separate and documented with 6-month retention.
9. Staging deploys from CI to the VPS, and a restore rehearsal has been executed at least once with its output captured.
10. The Must-NOT-Have table is fully respected (F4 passes).
