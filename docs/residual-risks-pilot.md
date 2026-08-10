# Pilot residual-risk adjudication

This is the controlled-pilot disposition of every residual carried by the
pilot-readiness plan. The historical record remains append-only in
[`docs/residual-risks.md`](residual-risks.md); this companion neither rewrites nor
restates that history. It records only the pilot classification, required action, and
the repository evidence that supports or will gate that classification.

The first three columns below carry the plan's §7 adjudication table verbatim. The
fourth column adds durable repository evidence pointers. A future control remains
classified as a mitigation or operator gate until its named pilot todo supplies that
evidence.

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
| §3.3 Redis ConnectionError→500 | MITIGATE BEFORE PILOT | PILOT-206: narrow fail-closed 429 at exceeds(); test-first; no auth bypass, no enumeration delta, no infra detail leak, auto-recovery, Sentry visibility. | The current measured behavior remains in `tests/security/test_rate_limit_isolation.py` and [`docs/residual-risks.md`](residual-risks.md); PILOT-206 must replace it with its fail-closed pin before pilot. |
| §4.1/4.2/4.3 Invitation scope semantics | ALREADY RESOLVED / ACCEPTED | Documented behavior inherited; 410 lifecycle disclosure stays (deliberate, tested). | `tests/accounts/test_invite_route_scope.py` pins live wrong-door indistinguishability and deliberate dead-token disclosure; `tests/accounts/test_invite_revocation.py` pins withdrawal scope. |
| §5.1 Comment-reference guard limit | ACCEPTED | Inherited honestly; new docs keep citations resolvable (guard scans docs). | `tests/scope/test_comment_references.py` scans this document and proves existence, not semantic currency. |
| §5.2 Public signup closed | ALREADY RESOLVED | Walkthrough re-verifies (step 14). | `tests/accounts/test_signup_surface.py` pins the closed route on both host families. |
| §6.1 .git/info/exclude local-only | ACCEPTED + documented | PILOT-001 documents portability rule; no CI clean-worktree gate added. | The boundary is recorded in [`docs/residual-risks.md`](residual-risks.md); PILOT-001 owns the portable operator documentation. |
| §6.2 Staging release inferred | MITIGATE BEFORE PILOT | PILOT-002 observes now; PILOT-202 makes it observable forever (/versionz + public deploy assert). | `.github/workflows/ci.yml` is the current inferred deploy path; PILOT-002 and PILOT-202 must add observed evidence before pilot. |
| §6.3 TOTP flake | MITIGATE BEFORE PILOT | PILOT-006 deterministic fix; single red re-run diagnostically only until fix lands. | The pinned-clock helper is in `tests/support.py`; its login consumers include `tests/accounts/test_invite_revocation.py` and `tests/accounts/test_member_lifecycle.py`. |
| §7 guards' reach limits | ACCEPTED | Inherited; F4 must not claim broader coverage. | `tests/scope/test_comment_references.py`, `tests/scope/test_status_literals.py`, and `tests/ui/test_status_render_policy.py` state and pin their deliberately narrow reach. |
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
