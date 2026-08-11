# Pilot residual-risk adjudication

This is the controlled-pilot disposition of every residual carried by the
pilot-readiness plan. The historical record remains append-only in
[`docs/residual-risks.md`](residual-risks.md); this companion neither rewrites nor
restates that history. It records only the pilot classification, required action, and
the repository evidence that supports or will gate that classification.

The first three columns below carry the plan's §7 adjudication table verbatim, including
its classifications. They are the adjudication as made and are not rewritten as work
lands. The fourth column is this document's own contribution: durable repository evidence
pointers, and the current state of each control.

Read the fourth column carefully, because "the code landed" and "the risk is closed" are
different claims and several rows are deliberately between the two. A row is marked:

- **LANDED** when the repository change exists at the reviewed head and its pins are green.
- **HALF LANDED, HALF STILL OPEN** when the mechanism exists but the evidence that closes
  the residual is external — a real deploy, a real bucket, a real mailbox, a host setting.
- unqualified when the named todo has not delivered anything yet.

No operator gate is ever closed by a repository change alone. A control stays classified
as a mitigation or operator gate until its named pilot todo supplies the evidence, and
landing the code does not advance the classification.

| Residual | Classification | Basis / action | Repository evidence |
| --- | --- | --- | --- |
| §1.1 Browser history (URL bearer credentials) | ACCEPTED FOR CONTROLLED PILOT | Proofs pinned by PILOT-007: GET consumes nothing (invite accept mutates POST-only views.py:324-398; installed allauth CONFIRM_EMAIL_ON_GET default False app_settings.py:402-403 + new pin test; reset uses set-password session flow); credentials 7-day expiry (INVITE_VALIDITY), single-use (accepted_at + token digest unique); logs/Referer scrubbed (measured); runbook explains prefetch/scanner behavior; resend/revoke safe (revoke pinned tests/accounts/test_invite_revocation.py). Clean-URL redesign stays out of scope — NOT a blocker because no GET consumes. | `tests/accounts/test_credential_get_semantics.py` pins all four GET contracts; firm guards are `_accept_as_signed_in_user` and `_accept_as_new_account` in `apps/accounts/views.py`; portal guards are `_accept_as_signed_in` and `_accept_as_new_account` in `apps/portal/invite_views.py`; lifecycle and revocation remain pinned by `tests/accounts/test_invite_revocation.py`. |
| §1.2 Email scanners / link previews | ACCEPTED FOR CONTROLLED PILOT | Same proofs; observation (not consumption) remains inherently uncontrollable; runbook + support procedure (PILOT-502). | `tests/accounts/test_credential_get_semantics.py` proves automated GETs do not consume any of the four credential-route variants. Operational handling remains a PILOT-502 gate. |
| §2.1 Caddy log | ALREADY RESOLVED | Redacted + re-measured zero; no action. | `ops/Caddyfile` carries the redaction; the surviving application-side canaries are in `tests/security/test_credential_log_hygiene.py`. |
| §2.2 Django logs | ALREADY RESOLVED | CredentialURLLogFilter; regression tests live. | `CredentialURLLogFilter` is in `config/settings/prod.py`; `tests/security/test_credential_log_hygiene.py` is the regression suite. |
| §2.3 Docker log retention | OPERATOR GATE | PILOT-209 verifies/configures host rotation (driver, max-size/max-file, journald caps, permissions, post-rotation canary absence) on current AND target VPS (re-run in PILOT-402). | `docker-compose.prod.yml` defines the shipped services; host-driver retention remains external evidence owned by PILOT-209 and PILOT-402. |
| §2.5 Sentry scrubbing shape-dependence | ACCEPTED FOR CONTROLLED PILOT + guard | No SDK bump in this plan; runbook rule: any sentry-sdk upgrade re-runs the canary suite (tests/security/test_credential_log_hygiene.py) — recorded in PILOT-502. | The event scrubbers are in `config/settings/prod.py`; the required upgrade canaries resolve at `tests/security/test_credential_log_hygiene.py`. |
| §3.1 Per-endpoint DoS survives | ACCEPTED FOR CONTROLLED PILOT | Correct trade per record; monitoring adds 429-visibility (Sentry breadcrumbs/logs); no limit changes. | Endpoint isolation and the surviving per-endpoint limit are pinned by `tests/security/test_rate_limit_isolation.py`. |
| §3.2 15/m combined per-address | ACCEPTED FOR CONTROLLED PILOT | Operator already accepted; restated in §29. | The independent address buckets and unchanged IP umbrella are pinned by `tests/security/test_rate_limit_isolation.py`. |
| §3.3 Redis gaierror→429 | ALREADY RESOLVED (fails closed) | Keep; test stays. | The measured `socket.gaierror` behavior is pinned by `tests/security/test_rate_limit_isolation.py`. |
| §3.3 Redis ConnectionError→500 | MITIGATE BEFORE PILOT | PILOT-206: narrow fail-closed 429 at exceeds(); test-first; no auth bypass, no enumeration delta, no infra detail leak, auto-recovery, Sentry visibility. | **MITIGATION LANDED.** `exceeds()` in `apps/security/ratelimit.py` catches `redis.exceptions.ConnectionError` and the builtin `ConnectionError`, calls `sentry_sdk.capture_exception()`, and denies — which `apps/security/middleware.py` renders as 429. The fail-closed pins are in `tests/security/test_rate_limit_isolation.py`; the dated correction is appended to [`docs/residual-risks.md`](residual-risks.md). |
| §4.1/4.2/4.3 Invitation scope semantics | ALREADY RESOLVED / ACCEPTED | Documented behavior inherited; 410 lifecycle disclosure stays (deliberate, tested). | `tests/accounts/test_invite_route_scope.py` pins live wrong-door indistinguishability and deliberate dead-token disclosure; `tests/accounts/test_invite_revocation.py` pins withdrawal scope. |
| §5.1 Comment-reference guard limit | ACCEPTED | Inherited honestly; new docs keep citations resolvable (guard scans docs). | `tests/scope/test_comment_references.py` scans this document and proves existence, not semantic currency. |
| §5.2 Public signup closed | ALREADY RESOLVED | Walkthrough re-verifies (step 14). | `tests/accounts/test_signup_surface.py` pins the closed route on both host families. |
| §6.1 .git/info/exclude local-only | ACCEPTED + documented | PILOT-001 documents portability rule; no CI clean-worktree gate added. | The boundary is recorded in [`docs/residual-risks.md`](residual-risks.md); PILOT-001 owns the portable operator documentation. |
| §6.2 Staging release inferred | MITIGATE BEFORE PILOT | PILOT-002 observes now; PILOT-202 makes it observable forever (/versionz + public deploy assert). | **HALF LANDED, HALF STILL OPEN.** The mechanism exists: `versionz` is served from `apps/core/views.py` and routed in `config/urls.py`, `.github/workflows/ci.yml` asserts it over the public edge against the deployed SHA, and `.github/workflows/verify-live.yml` re-checks it against the head of `main` on a schedule. **No live observation exists yet** — none of this has reached `main`, no deploy has run it, and PILOT-002's observed-release evidence is still owed. |
| §6.3 TOTP flake | MITIGATE BEFORE PILOT | PILOT-006 deterministic fix; single red re-run diagnostically only until fix lands. | **FIX LANDED.** The step-boundary determinism is in `tests/support.py` and pinned by `tests/test_support.py`; its login consumers include `tests/accounts/test_invite_revocation.py` and `tests/accounts/test_member_lifecycle.py`. |
| §7 guards' reach limits | ACCEPTED | Inherited; F4 must not claim broader coverage. | `tests/scope/test_comment_references.py`, `tests/scope/test_status_literals.py`, and `tests/ui/test_status_render_policy.py` state and pin their deliberately narrow reach. |
| NEW (found in review of PILOT-207): Celery worker events widen Sentry's stack-local capture | ACCEPTED AND RECORDED; owner decides at PILOT-504 | Adding `CeleryIntegration` extends capture from request handling to task execution, so `include_local_variables` (SDK default: on) can now serialize task-local objects — a `client` during a failed calendar sweep, for example — that no web request would have held. The existing denylist, both send hooks and `send_default_pii=False` still apply to every one of those events, which bounds the exposure rather than removing it. At 10-30 pilot clients the accepted view is that diagnostics on a broken worker are worth more than the residue. | The integration and every scrubber are in `config/settings/prod.py`; the scrubbing canaries are `tests/security/test_credential_log_hygiene.py`. **Nothing was changed for this row.** PILOT-504 already inspects a controlled event; it now also inspects a **worker-origin** one and either sets `include_local_variables=False` or folds this into the §2.5 Sentry-shape residual. |
| NEW (this plan): storage lifecycle unadjudicated (orphan bytes, no real-bucket proof, single bucket, on-host backups, no object DR) | MITIGATE BEFORE PILOT | PILOT-203/204/205/301/303 + §29 records what remains. | The shipped storage boundary is configured in `config/settings/prod.py`; real-bucket, lifecycle, orphan, off-host, and recovery evidence remains gated by PILOT-203, PILOT-204, PILOT-205, PILOT-301, and PILOT-303. |

## Credential-GET proof boundary

`tests/accounts/test_credential_get_semantics.py` proves only that a GET does not spend
an invitation, verify an email address, or invalidate the reset key held for the
set-password POST. It does not claim that browsers, mailbox scanners, proxies, or link
preview services cannot observe the credential. That observation risk is why §1.1 and
§1.2 remain accepted residuals rather than resolved findings, and why the clean-URL or
token-exchange redesign remains explicitly out of scope for the controlled pilot.

The reset proof follows the behavior actually installed: the credential-bearing GET
stores the validated key in the session and redirects to the non-secret `set-password`
URL; the POST submits there using that same session-held key. The `set-password` special
case in `config/settings/prod.py` is log-redaction handling, not the reset-flow
implementation.

## The Celery capture widening, and why nothing was changed for it

This one was found by review rather than by a todo, so the reasoning is written out here
rather than compressed into a table cell.

Before PILOT-207 the only Sentry integration was `DjangoIntegration`, so the events that
carried stack-frame locals were request-shaped, and every scrubber in
`config/settings/prod.py` was tuned against exactly those shapes. Adding
`CeleryIntegration` was the right call — a worker that dies silently is the failure mode
monitoring exists to catch — but it changes what an event can contain. Task frames hold
whatever the task held: an ORM instance, a `client` mid-sweep, a storage handle. The SDK
serializes those locals by default.

Three things bound it, and none of them removes it. The recursive denylist still redacts
the named credential fields. Both send hooks still scrub URLs, request data, transaction
names, breadcrumbs, spans and repeated credentials in stack-frame locals. And
`send_default_pii=False` still suppresses the SDK's own user and request attachment. What
survives is the residue: whatever a worker frame happened to be holding that none of
those rules names.

**No setting was touched.** `include_local_variables=False` would close it and would also
strip the variable values from every traceback, which is most of what makes a worker
stack trace worth having. That is a diagnostics-versus-privacy trade, it belongs to the
owner, and at 10-30 pilot clients it is not obvious in either direction. PILOT-504 is
already the place a controlled event gets inspected; it now inspects a worker-origin one
too, and the decision gets made against a real payload instead of against this paragraph.
