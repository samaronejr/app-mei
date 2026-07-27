# Solo Build Plan — Multi-Tenant SaaS for Accounting Firms + Paired MEI Portal

**Basis:** `deep-research-report.md` (product thesis, market benchmarks, integrations, compliance research). **Delta:** this plan re-cuts that specification for a single developer building with AI-assisted tooling. It makes the decisions the report leaves open — stack, build-vs-buy positions, sequencing — and replaces the team-shaped budget with a solo-shaped one.

The report's product thesis stays fully intact: an **accountant-first control plane** (portfolio, deadlines, permissions, audit) paired with a **simple MEI self-service portal**, winning in the gap that MEI Pronto, MaisMei, and Awise leave open.

---

## 1. What changes when the builder is one person

The report prices a ~7-person delivery team at R$ 1.27–2.63m for the first year. A solo build converts that budget into calendar time, which forces three structural changes:

**The currency is your hours, so the stack must be the boring part.** The irreducibly hard part of this product is the Brazilian fiscal domain — DAS/DASN workflows, NFS-e Nacional, CNPJ alfanumérico readiness, LGPD posture, and the portal-assist realism the report correctly insists on. Every hour spent learning a new framework is an hour not spent encoding domain rules. This drives the stack decision in §2.

**Integration ambition must be staged, not front-loaded.** The report's "three modes per connector" (native API / provider API / guided portal) is the right pattern — but a solo MVP should *start* nearly everything in portal-assist mode with structured evidence capture, and graduate a connector to provider or native mode only after pilot firms prove which automations they will actually pay for. Portal-assist is not a stub here; it is a first-class implementation (§3.3).

**Django admin, managed services, and AI-assisted coding replace headcount.** The platform-admin console, back-office CRUD, and ops tooling come nearly free from Django admin. Payments, error tracking, object storage, and backups are managed services. Test scaffolding, connector adapters, and fixture generation lean on a spec-first Claude Code workflow (§9).

---

## 2. Stack decision

**Recommendation: a Django 5.2 LTS modular monolith — not the report's TypeScript stack.**

The report's Next.js + NestJS recommendation is reasonable *for the team it assumes*: its headline benefit (shared types/DTOs across frontend and backend developers) is a team-coordination benefit. Solo, it costs you two codebases, two deploy targets, and a framework you'd be learning while also learning the NFS-e Nacional manual. Django gives you, out of the box, the things the report budgets people for: an admin interface, a mature auth system, ORM migrations, forms, pt-BR localization and timezone handling, and a security baseline. Your existing depth in Python — and the multi-tenant/LGPD groundwork already thought through for Clinic OS — transfers directly. Python is also the better home for the report's later-stage roadmap (categorization models, anomaly detection, portfolio analytics).

| Layer | Choice | Notes |
|---|---|---|
| Backend | Django 5.2 LTS on Python 3.12 | LTS support into 2028; modular monolith with strict app boundaries (§3.2) |
| Database | PostgreSQL 16 | Row-level security is central to the isolation model (§3.1) |
| Frontend | Django templates + HTMX + Alpine.js + Tailwind | One codebase; server-rendered; SPA-feel where it matters (queues, dashboards) |
| Client portal | Same stack shipped as installable PWA | Manifest + service worker + web push (iOS supports push for installed PWAs) |
| Async | Celery + Redis; django-celery-beat | Reminder sweeps, due-date generation, imports, connector jobs |
| Auth | django-allauth + django-otp (TOTP MFA) | gov.br OIDC deliberately deferred (§4) |
| Storage | S3-compatible (Cloudflare R2 / Backblaze B2) | Per-tenant prefixes, signed URLs only |
| Payments | Asaas-class PSP | Pix/boleto/card + webhooks + sandbox; used both for firm fee-billing *and* your own subscriptions |
| Observability | Sentry free tier + structured logging | OpenTelemetry only when scale demands it |
| Dev & deploy | Docker Compose everywhere; GitHub Actions CI; VPS (Hetzner-class) or PaaS (Fly.io/Railway) | Pay for PaaS only if you'd rather buy back ops hours |

**What survives from the report unchanged:** the module boundaries (§3.2 mirrors its module table), the tenant-first data model, RLS enforcement, the three-mode connector abstraction, the PWA client portal, pt-BR/BRT-aware scheduling, WCAG 2.2 AA intent, and the audit posture. If a funded team ever materializes, the monolith's module seams are the extraction map — nothing below paints you into a corner.

---

## 3. Architecture

### 3.1 Tenancy model

**Shared schema with `tenant_id` on every business table** — not schema-per-tenant (django-tenants). Rationale: this product expects many small tenants, needs platform-level cross-tenant reporting, and a solo dev cannot afford N-schema migration runs. Isolation is enforced in three layers:

1. **Application layer.** A `TenantScopedManager` as the default manager on every business model, with the current tenant resolved by middleware (subdomain or session). Unscoped access requires an explicit `.all_tenants()` call — rare, intentional, and grep-auditable.
2. **Database layer.** PostgreSQL row-level security as defense-in-depth. With `ATOMIC_REQUESTS=True`, middleware issues `SET LOCAL app.tenant_id = '<uuid>'` inside the request transaction; policies use `USING (tenant_id = current_setting('app.tenant_id')::uuid)`. The app connects under a role without `BYPASSRLS`; migrations run under a separate role. A CI test proves cross-tenant reads fail.
3. **Object storage.** Per-tenant key prefixes; every download flows through a permission check that mints a short-lived signed URL.

The second scoping axis — staff accountants seeing *assigned clients only*, per the report's RBAC matrix — is application-layer. Implement the matrix as data (a permissions table) behind a single `can(user, action, obj)` entry point, so the whole matrix is unit-testable and future role tweaks don't touch view code.

### 3.2 Project layout — Django apps ↔ report modules

| Django app | Report module | In MVP |
|---|---|---|
| `accounts` | Registration/onboarding (users, MFA, invites) | ✅ |
| `tenants` | Multi-tenant firm admin (firm, plan, seats) | ✅ |
| `clients` | Client registry & portfolio (companies, assignments, tags, onboarding checklist, readiness score) | ✅ |
| `obligations` | Tax calculation/DAS + DASN (rule engine, calendars, evidence) | ✅ |
| `invoices` | Invoicing (registry, request workflow, artifacts, connector) | ✅ portal-assist |
| `documents` | Document management (vault, retention rules) | ✅ |
| `portal` | Client portal (PWA views) | ✅ |
| `notifications` | Notifications (email, web push, templates) | ✅ |
| `banking` | Bookkeeping-lite (OFX/CSV import, transactions, reconciliation) | ✅ import-first |
| `billing` | Payments & billing (Asaas: firm fees + platform subscriptions) | ✅ |
| `reports` | Reports & analytics (portfolio KPIs, exports) | ✅ v1 |
| `integrations` | Integrations hub (connector framework, connection state, health) | ✅ framework only |
| `audit` | Audit logs + Marco Civil access-log stream | ✅ |
| `support` | Support console | ❌ use an external helpdesk at first |
| `payroll` | eSocial payroll | ❌ out of MVP entirely |

### 3.3 Connector pattern

One interface, three modes, exactly as the report prescribes. Concretely: `Connector` classes declare capabilities (`issue_invoice`, `fetch_das_status`, …) and a mode (`native | provider | portal_assist`). The portal-assist implementation renders guided steps, deep links, evidence-upload slots, and writes the *same normalized records and raw artifacts* the API path would. That symmetry is what makes graduating a connector later a swap, not a rewrite — and it keeps the product honest about e-CAC and REGULARIZE, which the report confirms are portal-centric today.

---

## 4. Build-vs-buy and integration sequencing

The rule the report lands on — *official where stable, provider where fragmentation is expensive, portal-assist where public APIs are absent* — plus one solo amendment: **a connector only graduates modes when pilot evidence shows firms will pay for the automation.**

| Integration | MVP mode | Graduation path | Notes / to validate |
|---|---|---|---|
| Identity & login | Email + password + TOTP MFA; invite links | gov.br OIDC post-pilot | gov.br login for private platforms requires credenciamento and carries per-authentication costs — validate current terms before committing. Meanwhile the onboarding checklist still *records* each client's gov.br trust level (bronze/prata/ouro) as data, per the report. |
| DAS (monthly) | Calendar + reminders + PGMEI deep link + payment-proof upload + status tracking | Provider API for automatic status/guide retrieval (Infosimples-class, pay-per-query) | Automation cost per client per month must be far below what firms pay you. Measure willingness in pilot (§13). |
| DASN-SIMEI (annual) | Guided checklist + portal-assist + evidence capture | "Season mode" batch workflow before the first May you have real users | The last-business-day-of-May deadline is the product's biggest annual retention event. |
| NFS-e | Invoice registry + request workflow; client issues on the national emitter (gov.br login, no certificate) via guided steps; XML/PDF captured into the vault | Provider (Nuvem Fiscal-class) or native National API | The native API path implies digital-certificate handling that most MEIs don't have — validate per segment before building it. Seed a municipality capability registry from day one, even as mostly-static data. |
| NF-e / NFC-e | ❌ not in MVP | Provider abstraction only if pilot portfolios skew commerce | Aligns with the report: don't chase Awise's ERP depth. |
| Bank data | OFX/CSV import + rule-based categorization + reconciliation screen | Pluggy/Belvo-class Open Finance post-pilot | Provider per-connected-account fees change margin math — decide after pricing is validated. |
| Pix/boleto billing | Asaas from day one, both directions (firm fees to clients; your subscriptions to firms) | Recurring Pix, dunning automation later | Webhooks + sandbox make this the easiest high-value integration in the whole plan. |
| e-CAC / PGFN REGULARIZE | Structured case objects + deep links + notes + attachments (portal-assist only) | Stays portal-assist | Keep the report's discipline: no undocumented-API adventures against government portals. |
| eSocial payroll | ❌ | Only if pilot firms have employer-MEIs in real volume | Official rule: one employee max; most clients won't need it. |
| WhatsApp | ❌ (email + PWA web push first) | Provider later, deadline nudges as the use case | The report itself gates messaging behind stable core workflows. |

---

## 5. MVP re-cut (against the report's MVP table)

The report's bar is the right one and worth keeping verbatim in spirit: **the MVP must make an accounting firm operationally better within one tax cycle.**

| Disposition | Items |
|---|---|
| **Keep as specified** | Firm tenant + users + roles (full RBAC matrix), client workspaces, onboarding checklist with gov.br/certificate readiness score, accountant dashboard + work queues, client portal, DAS calendar + evidence workflow, DASN workspace, document vault, notifications, portfolio report v1, fee billing via Pix/boleto |
| **Keep but simplify** | Invoicing → registry + request + portal-assist issuance + artifact capture (API automation deferred). Bookkeeping → import-first reconciliation with categories and a **monthly revenue vs. R$ 81.000 threshold monitor** (proportional in the opening year, ≈ R$ 6.750 × months) instead of a full ledger/chart-of-accounts UI. The threshold monitor is the highest-value simple feature in the whole bookkeeping area. |
| **Defer** | Open Finance sync, NF-e/NFC-e, payroll, white-label portal, advanced analytics, in-product support console, WhatsApp |

---

## 6. Data model notes

Adopt the report's ER model minus the payroll tables. Concrete implementation decisions:

- **IDs:** UUIDv7 primary keys, generated app-side. `tenant_id` is denormalized onto every business table even where derivable — RLS policies need it locally.
- **CNPJ/CPF:** store normalized uppercase alphanumeric `CHAR(14)` / `CHAR(11)`. Validators must implement the CNPJ alfanumérico check-digit algorithm (weights over ASCII value − 48); confirm your validation library's current version supports it, otherwise implement from the published spec (~30 lines) and property-test it. Masks, search inputs, exports, and connector payloads must all be alphanumeric-ready — the report is right to treat this as a hard requirement, since it is already in production for new registrations.
- **Money:** `DECIMAL(14,2)`, BRL only in v1. **Dates:** store UTC, compute all scheduling in `America/Sao_Paulo`. The due-date generator implements "day 20, rolled forward past weekends/holidays" against a national holiday table (municipal holidays later).
- **Fiscal artifacts:** immutable originals (XML, PDF, payment receipts) in object storage, plus normalized searchable rows — the report's evidentiary-integrity split. Originals are never mutated; corrections create new records.
- **Audit:** `audit.Event` is append-only (database grants allow INSERT/SELECT only), covering the report's critical-action list (logins, MFA changes, role changes, assignment changes, connector connect/disconnect, invoice attempts, DAS confirmations, declaration submissions, exports, impersonation). A **separate access-log stream retained six months** satisfies the Marco Civil requirement and is documented apart from business-audit retention.
- **The obligation engine is data, not code.** Obligation types, due-date rules, and MEI parameters (R$ 81.000 ceiling, DAS day-20, DASN deadline, one-employee flag) live in versioned parameter tables. Limits and dates change politically; the engine shouldn't need a deploy when they do.

---

## 7. Security & LGPD baseline (solo-sized, non-negotiable)

- **MFA (TOTP) mandatory** for all firm-side roles from day one; optional for MEI clients in v1.
- **Isolation:** the three layers in §3.1, with the cross-tenant-denial test running in CI on every PR.
- **Secrets & tokens:** environment/secret-manager only, never in the repo. Provider tokens encrypted at rest in a table separate from user sessions (a compromised web session must not expose connector credentials — the report's separation requirement). If ICP-Brasil certificate custody ever enters scope, that is a separate design review; v1 stores certificate *metadata* only.
- **Backups:** point-in-time recovery (WAL archiving) + nightly logical dumps to a second provider + a **monthly restore rehearsal** into staging. A backup you haven't restored is a hypothesis.
- **LGPD:** mirror the report's role split into your contracts — you act as *operator* for firm-service data and *controller* for platform analytics/security telemetry. Map lawful bases per workflow (legal obligation and contract performance carry most tax workflows; consent only where genuinely required, e.g., future Open Finance). Data-subject-rights intake is a form plus a documented manual process — fine at this scale. Prepare the "cannot delete yet: legal/tax retention applies" communication template. You are the named encarregado (DPO), with a one-page incident runbook including the ANPD notification path. Retention schedule: fiscal artifacts ≥ 5 years, access logs 6 months, the rest per policy.
- **Hardening:** Django's `SECURE_*` settings, CSP, per-tenant rate limiting, short-lived signed URLs, Dependabot plus a fixed monthly patching slot.

---

## 8. Roadmap and effort (solo)

| Phase | Scope | Focused hours |
|---|---|---:|
| **0 — Foundations** | Repo, Docker Compose (web/worker/beat/db/redis), CI, staging + prod deploy pipeline, allauth + TOTP, tenant core (Tenant/Membership/Invite), RLS harness + CI isolation test, Django admin, audit skeleton | 40–60 |
| **1 — Portfolio core** | Client registry + assignments + RBAC-as-data, onboarding checklist + readiness score, obligation engine (parameterized rules, 12-month DAS calendar, business-day logic), work queues, accountant dashboard v1 | 90–130 |
| **2 — Client side** | Portal PWA (tasks, documents, obligations view), document vault + retention rules, notifications (email + web push), full DAS evidence loop | 80–120 |
| **3 — Money + invoices** | Asaas integration both directions, invoice registry + request workflow + portal-assist issuance + artifact capture, reports v1, audit UI, CSV client-import tool | 90–130 |
| **4 — Pilot** | 1–2 friendly firms, parallel-run one full monthly cycle, fixes, OFX/CSV bank import + reconciliation v1, onboarding playbook | 70–110 |
| **Total to piloted MVP** | | **≈ 370–550 h** |

At 15–20 focused h/week alongside the master's, that is roughly **5–7 months**; full-time, **3–4 months**. Two sequencing rules: nothing in phase N+1 starts until a real firm can complete phase N's loop end-to-end in staging; and work backward from the two immovable dates — **day 20 of every month** (the DAS loop must be excellent) and **the last business day of May** (DASN season mode must exist before your first May with real users — if GA lands by Q1, that first DASN season is your best retention event).

**Post-pilot backlog, ordered by pilot evidence, not by interest:** Open Finance via provider → NFS-e issuance automation via provider → DASN season batch mode → gov.br OIDC → WhatsApp nudges → NF-e abstraction.

---

## 9. The solo operating system

**Spec-first with Claude Code.** Each module gets a short spec (this document plus the relevant report section) before code. Connectors get contract tests against recorded fixtures. Fiscal rules get the report's "golden month-close" fixture packs, solo-sized: a service MEI, a commerce MEI, a mixed-revenue MEI, an opening-year proportional-limit case, a delinquent client, and an employer-MEI that the system correctly flags as out of scope.

**Two automated regression packs from day one** — exactly the two the report mandates: the **CNPJ alfanumérico pack** (validators, masks, search, exports, payloads) and the **municipality-variance pack** (NFS-e capability registry behavior).

**Regulatory watch, one-person edition.** A recurring monthly review of RFB / NFS-e Nacional / gov.br changelogs and MEI-limit news, logged into an in-product regulatory-calendar table. This is the report's two-cadence maintenance model (engineering cadence + regulatory cadence) collapsed to a sustainable solo ritual.

**Support.** You are L1, L2, and L3. Survive deadline peaks by design rather than heroics: proactive reminders, exception queues, templated responses, a public status page, and pilot expectations set in writing with generous response SLAs.

**Guardrails against solo failure modes.** Feature flags on every connector; a weekly deploy rhythm; a work-in-progress limit of one module; scope changes go to the post-pilot backlog by default, not into the current phase.

---

## 10. Running costs and pricing hypothesis

| Item | Monthly (approx., validate current prices) |
|---|---|
| VPS or PaaS | R$ 80–250 |
| Managed Postgres (or on-VPS with PITR) | R$ 0–150 |
| Object storage | R$ 5–30 |
| Transactional email | R$ 0–50 |
| Sentry, CI | free tiers |
| Domain | ~R$ 40/year |
| PSP fees | per transaction, mostly passed through on firm billing |
| **Total pre-revenue** | **≈ R$ 150–500/month** |

The report's R$ 1.27–2.63m first-year figure therefore becomes: your hours + roughly R$ 2–6k/year of infrastructure + provider fees that scale only with revenue.

**Pricing hypothesis** (the report's B2B2C model, packaged): **Starter** at R$ 149–199/month including ~15 active client workspaces, plus R$ 5–8 per additional active client; a **Growth** tier adding seats, DASN season mode, and priority support; embedded fee-billing margin later. Anchor: Awise's public tiers (R$ 59,90–219,90) price a *single-business* ERP — an accountant control plane can price above that per firm while remaining far below the cost of one staff-hour saved per month. Treat all numbers as hypotheses; after the first free cycle, pilots pay something non-zero, because a price of R$ 0 validates nothing.

---

## 11. Risks — solo edition

All risks in the report stand. These are the ones that are amplified or new when the team is one person:

| Risk | Mitigation |
|---|---|
| Bus factor = 1 | Boring stack, infrastructure-as-code, docs written as you build, monthly restore rehearsal, and an "if I disappear" runbook shared with pilot firms |
| Deadline-peak support crush | §9 guardrails; cap the pilot at ≤ 2 firms / ≤ 60 client companies until the first DAS cycle runs smoothly |
| Over-integration temptation | Portal-assist first; a connector graduates only with pilot evidence that someone pays for the automation |
| Regulatory drift (CNPJ format, NFS-e adoption, MEI limit changes) | Parameter tables (§6), regression packs (§9), monthly regulatory watch |
| Master's/doctorate collision | Phase gates are pause-safe: every phase ends in a stable, demoable state; the calendar slips before the scope does |
| Cross-tenant trust incident | Three-layer isolation (§3.1) with the RLS denial test in CI on every PR — in accounting, one leaked row is a churn event even if it never becomes a legal breach |

---

## 12. First two weeks — concrete

**Week 1:** repo + Docker Compose (web, worker, beat, Postgres, Redis); CI with lint, tests, and a migrations check; staging deploy pipeline; django-allauth + django-otp; `Tenant`/`Membership`/`Invite` models; RLS migration harness and the CI test proving cross-tenant reads fail.

**Week 2:** `ClientCompany` + assignments + the RBAC permissions module (matrix-as-data + tests); alphanumeric CNPJ/CPF validators with property tests; obligation-engine skeleton generating a 12-month DAS calendar with business-day rolling and a holiday table; everything registered in Django admin; deploy to staging; write the one-page pilot pitch for accounting firms.

**Exit criteria:** a firm tenant with two users and three fake clients sees a correct 12-month DAS calendar in staging, and the isolation test suite is green.

---

## 13. Validate with pilot firms before building more

1. Which municipalities dominate their MEI portfolios — and is national-emitter NFS-e coverage real for those cities? (Seeds the capability registry and the invoice-automation decision.)
2. Would they pay for automatic DAS status/guide retrieval at a per-query provider cost, or are reminders plus client-uploaded proof enough?
3. What share of their MEIs issue NFS-e monthly versus sporadically? (Drives whether invoice automation graduates early.)
4. Where do their clients bank, and does OFX export exist there — or is Open Finance sync needed earlier than planned?
5. What do they run on today (spreadsheets, WhatsApp, a competitor), and what is the one report they show clients monthly? (That report becomes your reports-v1 template.)
6. Do any of their MEIs employ someone? (Sizes real payroll demand before eSocial is ever considered.)
