# Residual risks after the residual-hardening plan

This is the honest remainder. The residual-hardening plan closed the credential log
sinks this repository controls, split the shared rate-limit fuse, pushed invitation
scope below HTTP and closed public signup. What follows is everything it did **not**
close, plus the shape of what it did close where that shape is narrower than a casual
reader would assume.

The rule for this page is that no sentence may claim more than its evidence proves.
Where a boundary is uncertain, it says so. Where a control was measured rather than
reasoned about, the measurement is named. The pilot-readiness work should inherit these
as facts, not re-derive them, and should not treat any of them as already handled.

`ops/README.md` holds the per-sink disposition table this page sits beside. Read that
table for "what is the current state of sink X"; read this page for "what is still
open, and how open is it".

---

## 1. Credential exposure that is not controllable from this repository

### 1.1 Browser history — NOT CONTROLLABLE

Invitation, email-confirmation and password-reset links carry their bearer credential in
the URL path. The credential therefore reaches the address bar, the browser's history
store, and any profile sync attached to it. `templates/base.html` also reflects
`request.get_full_path` into the htmx error-fallback retry link, so the credential URL
appears inside the rendered document as well.

Nothing server-side removes it. A token-exchange or clean-URL redirect **was evaluated
and rejected** for this plan (decision D8): it would rewrite an acceptance flow the
preceding, now-closed UI/UX plan pinned heavily with behavioural tests, and the Referer
half of the same problem was closable without it. That rejection is a scope decision,
not a finding that the redesign is unsound. If a future plan wants this closed, the
redesign is the way, and it is a real piece of work.

### 1.2 Email security scanners and link-preview services — NOT CONTROLLABLE

The token is mailed. That is the design. Recipient-side tooling — a mail gateway's URL
detonation sandbox, a chat client's link unfurler, a corporate proxy that prefetches —
can fetch the link before the human does. For a single-use invitation that means the
token can be consumed, or at minimum observed, by software this project neither
operates nor can detect.

Both 1.1 and 1.2 were recorded as `NOT CONTROLLABLE` in the ten-sink canary matrix
measured under RESIDUAL-008, and neither was claimed closed at any point.

---

## 2. Log sinks: what was measured, and what changed because of it

### 2.1 Caddy — the disposition changed from accept to redact

This is worth reading carefully, because an earlier stage of the work reached a
different answer.

RESIDUAL-005 took a **provisional measured-accept** for the Caddy log. Its reasoning was
that `ops/Caddyfile` runs at `level WARN`, Caddy emits ordinary access entries at INFO,
so nothing routine is recorded and only a 5xx would retain the request URI. The operator
approved that accept **conditionally** — RESIDUAL-008 had to prove the canary was absent
across four paths before it could stand.

It was not absent. The four-path measurement found the canary in the Caddy log on the
safe-error path, **2 hits**. So the accept was withdrawn and the sink was **redacted**,
not accepted. `ops/Caddyfile` now deletes `request>uri` and `request>headers>Referer` in
both the global error logger and the site access logger. Re-measurement across success,
refusal, rate-limit and safe-error found zero. A 502 still emits two diagnostic
receipts; they now carry no URI field.

Do not describe the Caddy sink as an accepted residual. Measurement overtook that
disposition. **It is the only sink in the plan that was ever permitted an "accept", and
it did not use it.**

### 2.2 Django application logs — a sink the plan believed clean

The plan's own credential-log design table said of Django application logs: "no path is
logged on the success path". That was true and beside the point. The four-path
measurement found the canary reaching the container log through `django.request` on
paths the success-path reasoning never covered: **1 hit on refusal and 6 on
rate-limit**. The rate-limit path was the largest single leak in the whole matrix, and
it only became measurable because the endpoint-specific 5/m bucket added under
RESIDUAL-006 made a 429 reachable in the trace at all.

It is now redacted by `CredentialURLLogFilter` in `config/settings/prod.py`, which
derives what to redact from the shared `CREDENTIAL_BEARING_URL_NAMES` registry — the
same source the audit-log redaction reads, so registering a route still redacts it
everywhere.

The general lesson, recorded because it will recur: reasoning about which sink records
what is not a substitute for driving a canary through it. Two of the ten sinks in this
matrix were wrong in the direction of "cleaner than it is".

### 2.3 Docker log retention — OPERATOR GATE, not a code control

Neither Compose file contains a `logging:` block. The default driver applies and
retention is host-level state. The *content* reaching that driver was measured clean on
all four paths after the Caddy and Django filters landed; the *retention* of whatever
does reach it is not something this repository can set or verify.

This is an operator gate, which is a different thing from a residual risk and a
different thing from a WONTFIX: there is nothing in-repo to fix, and there is something
outside the repo that must be done. The operator must configure and verify host-level
log rotation.

### 2.4 What "measured" means here

Every clean grep in the canary matrix is backed by a non-vacuity control that proved the
trace could detect a leak. Bypassing the Sentry hooks made the canary appear 18 times
across 3 records; bypassing the audit redaction produced exactly one raw row. Without
those controls a clean result would prove only that the trace was looking in the wrong
place. `tests/security/test_credential_log_hygiene.py` carries the regression coverage
that survives the teardown of the measurement stack.

### 2.5 Sentry scrubbing is shape-dependent

The scrubber in `config/settings/prod.py` targets today's event structure:
`request.url`, `request.data`, `request.headers`, breadcrumbs, spans and stack-frame
locals. It is written against the SDK's current serialization, not against an abstract
notion of "the event".

An SDK upgrade that moves, renames or restructures any of those fields would silently
reduce coverage. Nothing would fail loudly. The canary test is the only thing that would
catch it, and only if someone re-runs it after the upgrade. Treat a Sentry SDK bump as a
change that requires re-measuring, not as a routine dependency update.

---

## 3. Availability: what the rate-limit redesign did and did not buy

### 3.1 Per-endpoint denial of service SURVIVES

Endpoint-specific groups stop one flooded flow from locking the others. They do not stop
a determined attacker from denying **one** flow. An attacker willing to spend 5
requests/minute against `mfa-authenticate-ip` can keep that endpoint's bucket full for
that IP.

That is the accepted trade, and it is the correct one: the alternative to a per-endpoint
limit is no limit. What was eliminated is the *platform-wide* fuse, where flooding any
one address-less credential route refused all of them through the shared
`email:unknown` key. `tests/security/test_rate_limit_isolation.py` proves the isolation
pairwise across the address-less routes; it does not, and cannot, prove that a single
endpoint is undeniable.

### 3.2 The per-address axis is NOT additive — state the trade

On the per-IP axis the redesign is strictly additive: the `login-ip` umbrella survives
byte-identical at 20/m and still applies to every registered public POST, so
per-endpoint groups can only ever refuse more.

On the per-address axis it is **not** additive, and describing the whole redesign as
"strictly additive" would be false. Splitting the old shared `login-email` group into
`login-email` / `reset-request-email` / `dsr-email` raises one address's combined
allowance from a single 5/m bucket to three independent 5/m buckets — 15/m combined.

What that does and does not mean:

* Password **guessing** is still capped at 5/m, by `login-email` alone. The other two
  buckets carry no password.
* Aggregate traffic from one IP is still capped at 20/m, by the untouched `login-ip`
  umbrella.
* What was bought: a third party can no longer lock a victim out of their own login by
  filing DSR requests or password-reset requests in that victim's name.

The operator accepted this trade explicitly. It is recorded here rather than glossed
because "we split the buckets" reads like a pure improvement and it is not.

### 3.3 Redis failure behaviour, as measured rather than as intended

Two different failure modes, two different outcomes, both pinned by tests in
`tests/security/test_rate_limit_isolation.py`:

* `socket.gaierror` raised from `cache.add` → the request is refused with **429**. Fails
  closed, which is the intended direction.
* A generic `ConnectionError` **propagates** and surfaces as a **500**.

The second is not the designed behaviour; it is what django-ratelimit actually does,
because only `socket.gaierror` is caught on that path. It is recorded as measured. A
Redis outage of the second shape degrades credential endpoints to a server error rather
than to a closed door.

---

## 4. Invitation scope: severity, and the limit of indistinguishability

### 4.1 The domain-level scope parameter is defence in depth, not an incident fix

Invitation scope is now revalidated inside the acceptance transaction, after the row
lock and before any mutation. Read the severity correctly: **no reachable privilege
escalation existed through the current web routes before this landed.** Both HTTP doors
already refused a wrong-scope acceptance, and a direct domain call was already bounded
because `_client_seat` binds `client_id=invite.client_id`.

What the parameter adds is a bound on the caller's *host and entry-point expectation*,
which nothing previously enforced below HTTP, plus TOCTOU resistance inside the lock. It
protects future non-HTTP callers. A reader who finds this change in the history should
not conclude that an exploitable escalation was fixed, because one was not found.

Both view-layer guards were deliberately kept. They are what refuses a wrong-door GET,
which renders before any domain acceptance runs.

### 4.2 Indistinguishability is narrowed to REDEEMABLE invitations

The wrong-scope refusal is indistinguishable from an unknown token **only while the
invitation is still redeemable**. `resolve_invite` evaluates lifecycle before scope, so
an accepted, revoked or expired token answers **410** with a lifecycle-specific message
on both the wrong-scope and the right-scope path.

That is pre-existing behaviour, unchanged by this plan, and documented rather than
claimed away. It was not "fixed" by reordering the checks, because reordering would
change disclosure on every lifecycle path and that is a separate decision nobody made.
`tests/accounts/test_invite_route_scope.py` pins both halves: the live refusal is
indistinguishable, and the dead wrong-scope token keeps its lifecycle-specific gone
response.

### 4.3 `_visible_invite` remains tenant-scoped only, deliberately

The revocation lookup filters on tenant and not on client. That is not an oversight and
was left alone on purpose: issuance and revocation are gated identically by
`users.create`, so narrowing the lookup would withhold nothing from a caller who could
have created the row in the first place. `tests/accounts/test_invite_revocation.py` is
what holds that boundary.

---

## 5. What the guards prove, and what they do not

### 5.1 The comment-reference guard proves existence, never semantic currency

`tests/scope/test_comment_references.py` proves that a cited test module exists and
resolves. It cannot prove that the cited test still proves what the surrounding comment
claims. A comment repointed at a real but wrong test module passes. Semantic drift is
outside this check and must not be read as covered by it. The guard's own docstring says
the same thing; it is repeated here because this page is where a reader looks for
limits.

### 5.2 Public signup was CLOSED — the "accepted risk" branch does not apply

The plan described two dispositions for the open public-signup surface: close it, or
register-and-limit it and record the accepted mail-amplification and unassociated-user
risk here. **The operator chose to close it.** So there is no accepted signup risk to
record, and any later reader looking for one should stop looking.

What shipped: an `ACCOUNT_ADAPTER` (`apps/accounts/adapter.py`) returning `False` from
`is_open_for_signup()`, wired on both hosts. The refusal contract is pinned to the
literal values allauth actually serves — HTTP **200** rendering the template *name*
`account/signup_closed.html`, which allauth ships and this repository does not override,
and not 403 and not 404 — in `tests/accounts/test_signup_surface.py`. The signup URL was not
removed and allauth was not unmounted, so login, password reset and email confirmation
are untouched, and both invitation journeys still create accounts.

`account_signup` is a **static** route, so it would never appear in the credential-route
registry's dynamic-parameter walk. Its adjudication lives in its own test, by design,
not as a gap in `tests/scope/test_credential_routes.py`.

---

## 6. Process boundaries a future plan will trip over

### 6.1 `.git/info/exclude` is LOCAL-ONLY

`.omo/` and `.opencode/` are excluded through `.git/info/exclude`, which is not shared.
On CI, or in a fresh clone, those directories appear untracked.

The consequence is specific: a "clean worktree" gate defined as an **empty**
`git status` is **not portable**. The gate this plan ran at Wave 0 is a local developer
gate, not a CI invariant, and writing it into CI as-is would either fail immediately or
require an exclusion that does not currently exist in a tracked file.

### 6.2 The deployed staging release is inferred, not observed

No live host was contacted at any point during this plan. The staging release is
believed to be the commit CI last deployed successfully, by inference from a green
`deploy-staging` run. Nothing here observed it. Any pilot-readiness step that depends on
knowing what staging is actually running must go and look.

### 6.3 The TOTP flake is real, and it fired twice

Tests that perform a real TOTP login can fail on a time-step boundary. It happened twice
during this plan, both times on a test that logs in twice and compares the two results.
Neither was a regression; both passed on re-run and in isolation.

**A single red on a test doing a real TOTP login must be re-run before it is believed.**
The exact-equality assertion in the portfolio-growth ratchet must not be loosened to
work around it — that equality is the O(1) guarantee.

---

## 7. WONTFIX ledger

Five todos were explicitly permitted a documented WONTFIX, and one sink was permitted a
measured accept. **Essentially none were taken.** Recorded per todo, because a permitted
escape hatch that went unused is worth as much to a later reader as one that was used.

| Todo | What was permitted | What actually happened |
| --- | --- | --- |
| Invitation scope below HTTP | A measured WONTFIX, keeping the view-layer guards as the sole enforcement point | **SHIPPED.** The operator directed shipping the domain-level check. Severity remains hardening, per §4.1. |
| Client-workspace invitation list | A WONTFIX if the list could not fit the unchanged `DETAIL_QUERY_BUDGET = 60` | **SHIPPED at +1 query** (37 → 38). The budget in `tests/ui/test_clients_pages.py` is unchanged; headroom went 23 → 22. |
| `portfolio_scope` query reduction | A measured WONTFIX | **OPTIMIZATION SHIPPED.** Both-FULL path 4 → 3 queries; `QUERY_BUDGET` in `tests/ui/test_clients_pages.py` moved 27 → 26, downward. No budget in the repository rose. |
| Comment-reference guard | A WONTFIX for the `docs` root, and another for dotted-module checking | **NEITHER taken.** `docs` is scanned (all 5 citations resolve) and dotted-module checking is implemented for prose. |
| Quoted-status-literal guard | A WONTFIX if it produced unavoidable false positives, in which case the "keyed off enum members" constraint would have had to be deleted from the plan | **NOT taken.** It shipped clean in `tests/scope/test_status_literals.py`, so that constraint is enforceable rather than aspirational. |
| Caddy access/error log | A measured, explicitly accepted residual — the only sink permitted one | **Overtaken by measurement and became a redaction.** See §2.1. |

One consequence of the quoted-status-literal guard shipping is that the plan's "keyed
off enum members" rule is enforceable. The two presentation guards it sits beside are
policy-shaped rather than blocklist-shaped — but their reach is not uniform, and the two
halves have to be separated because only one of them is strong.

A future enum **member** genuinely is caught. The exhaustive key-set guards assert that
each map's key set EQUALS `{m.value for m in <Enum>}`, so adding a member without
mapping it reds the suite rather than falling through to a fallback word.

A future render **site** is caught only if it renders through one of the registered
policy filters or names a known `get_*_display` helper. That is what the guard was asked
to do — RESIDUAL-015 scoped it to a registry of status-render sites with `get_*_display`
banned at those sites — and it is what it does. The limitation is the other side of the
same mechanism: `tests/ui/test_status_render_policy.py` discovers a site by matching a
known filter or a known forbidden helper in its markup, so a brand-new template
rendering a bare raw status expression matches neither, is never discovered, and is
never checked. Reviewer F4 demonstrated this by running the guard's own detector against
the current template set plus a synthetic template containing a raw status expression:
the detector returned no offenders.

So: the guard proves that every **registered** render site renders through the filter
family, and that every enum member is mapped. It does not prove that every status
reaching a reader on a firm screen passes through a registered site. Extending it to a
generic raw-attribute scan was deliberately not done here — it would false-positive on
the `data-portfolio-<status>` tiles that legitimately carry stored values and on the
portal templates this plan holds out of scope — so the gap is recorded rather than
papered over. This is a claim about those three guards and about nothing else.

---

## 8. What this page does not cover

It covers the residual-hardening plan's own remainder. It is not a threat model, not a
pilot-readiness checklist, and not a statement about surfaces neither plan examined.
Absence from this page means "not adjudicated here", never "safe".
