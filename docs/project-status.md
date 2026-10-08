# Project status

What governs the work right now, which plans are finished, and where the durable
records live. This file is the tracked truth. If anything else in the repository
disagrees with it, this file wins and the other file is stale.

## Planning artifacts are local-only, on purpose

Work plans live under `.omo/plans/`, and `.omo/` is listed in `.git/info/exclude`.
That exclusion is deliberate: agent working state must not be committed, and it must
not pollute Tailwind's source detection. Consequences worth stating plainly:

- No plan file, notepad, or `boulder.json` is tracked. A clone of this repository
  contains none of them — except the six oldest `.omo` files (the phase-2a/2b
  plans and drafts, plus `accounting-mei-saas`), which were committed before the
  exclusion existed and stay tracked as frozen history. Every plan since,
  including both pilot-readiness plans, is local-only.
- An index placed inside `.omo/` could only ever be committed with `git add -f`.
  We do not do that. This document lives under `docs/` instead, which is why it is
  the index rather than a `.omo/plans/README.md`.
- The plan files themselves are immutable history once their work closes. They are
  never edited to reflect later reality. Later reality is recorded here.

Because plans are not in the clone, the sections below carry enough status for a
reader who will never see them.

## Current plan

Two plans are current, and they run on separate tracks: `pilot-readiness-v2`,
described next, and `axorim-pivot-r1`, described under "Axorim pivot" below.

**`pilot-readiness-v2`** — in execution. It supersedes `pilot-readiness-v1` and the
Resend amendment, and it starts from what is actually true rather than what v1
assumed: the latest code has never been deployed anywhere, the old OCI host still
serves a month-old release, no email has ever been sent, and the live-verification
workflow has been red for weeks. It ends with one accounting firm able to start a
controlled pilot on the new Lightsail server, every safety claim proven by a real
effect rather than asserted. 41 numbered rows across six waves, plus four final
verification passes.

Row identifiers from this plan use the `PILOT2-###` form in commit bodies and
evidence filenames. Rows carry a marker: `[E]` is an engineering row, `[O]` is an
operator gate that no repository change can close, and `[A]` is an agent-executed
external effect. Two owner decisions shape the whole plan: email goes through
Resend's plain SMTP relay (no new email code), and the deploy target is the
already-provisioned Lightsail instance `app-mei-pilot-sa-east-1` at 18.229.187.66,
with the OCI host kept intact after cutover as the rollback anchor.

All 41 rows are closed, including the operator-run Lightsail Phase B (row 16) and
the deploy-pipeline cutover (row 17, merged as PR #21, which triggered the first
deploy to Lightsail). The go/no-go ledger reports every S-line GO on the final
capture. What remains is the F1-F4 final-verification wave and, after it, the
owner's authorization of the pilot start; the `v0.2.0-rc1` tag and release follow
that authorization per `ops/RELEASES.md` Phase B.

## Axorim pivot

The product direction changed on 2026-09-27. Product work is now governed by the
local-only plan **`axorim-pivot-r1`**, which, like every plan since the exclusion,
lives under `.omo/plans/` and is never committed. "Axorim" is an internal working
name. The repository, the README headline and customer-facing copy stay "app-mei"
until the name is cleared.

- **Ids.** The plan has 29 original numbered todos in five waves, the added
  `AXR1-030` certificate guard, and a final verification wave. Todo N is cited
  as `AXR1-NNN` (todo 1 is `AXR1-001`) in commit bodies and in
  evidence filenames of the form `.evidence/AXR1-NNN-<kind>.txt`.
- **Delivery.** One open pull request per todo in a linear stack: the first is
  based on `main`, and each later branch and PR is based on the preceding todo's
  branch. No merges or force pushes; merging awaits the user's instruction
  because it deploys to the pilot host. Every new surface stays behind a
  per-tenant feature flag that's off by default. Each pull request updates the
  progress line below and the suite baseline.
- **Records.** `docs/product/pivot-supersession.md` says which earlier restrictions are
  superseded and which still bind. ADRs 0002 to 0007 in `docs/decisions/` hold the
  architecture decisions, and `docs/product/vocabulary.md` holds the status words.
- **Pilot obligations stay open and separate.** The `pilot-readiness-v2` F1-F4 wave
  and the owner's authorization of the pilot start are neither closed, waived nor
  absorbed by the pivot. They're decided on their own track. R1 reaches the pilot
  tenant only behind its flag, and only after that authorization.

Progress: `AXR1-001` (the pivot records) lands with the commit that adds this
section. `AXR1-002` (per-tenant feature flags with a global kill switch;
`tests/core/test_flags.py`; `.evidence/AXR1-002-red.txt`, `-happy.txt`,
`-failure.txt`, `-gates.txt`, and `-adversarial.txt`), `AXR1-003` (the R1 design
tokens, `templates/partials/ui/` components and the flagged product shell, plus the
browser and accessibility harness; `tests/ui/test_product_shell.py`,
`tests/ui/test_ui_partials.py`, `tests/test_support_threads.py`,
`tests/browser/test_shell.py` and `tests/browser/test_components.py`;
`.evidence/AXR1-003-baseline.txt`, `-happy.txt`, `-desktop.png`, `-mobile.png`,
`-failure.txt`, `-failure-role.txt`, `-flagoff.txt`, `-mutation.txt`,
`-ci-browser-job.txt`, `-gates.txt`, and `-adversarial.txt`), `AXR1-004` (the 13
S8.1 capabilities and per-user capability grants; `tests/authz/test_r1_capabilities_positive.py`,
`tests/authz/test_user_capability_grant.py`; `.evidence/AXR1-004-red.txt`,
`-happy.txt`, `-failure.txt`, `-gates.txt`, and `-adversarial.txt`) and `AXR1-030`
(certificate metadata-only guard; `tests/clients/test_certificate_metadata_only.py`;
`.evidence/AXR1-030-happy.txt`, `-failure.txt`, `-gates.txt`, and `-adversarial.txt`) are
recorded. `AXR1-005` (the client spine: `Engagement`, the effective-dated
`TaxProfile` and `ClientContact` with the provenance-marked backfill;
`tests/clients/test_tax_profile_transition.py` and
`tests/clients/test_backfill_v1.py`; `.evidence/AXR1-005-red.txt`, `-happy.txt`,
`-failure.txt`, `-failure-ext.txt`, `-failure-template1.txt`, `-plan.txt`,
`-adversarial.txt`, `-gates.txt`, and `-baseline.txt`) is recorded, with OG-10 —
the pilot database's one-time `CREATE EXTENSION` step — still open. `AXR1-006`
(the profile-driven calendar and threshold readers, revision-checked registry
dual-write, and client-isolated payment observations) is recorded on the T5
stack. Pilot payment rendering uses observation provenance and verification,
never work status; manual claims stay neutral. The gate repairs cover stale
admin POSTs, portfolio authorization before matching-profile returns, shared
audit-function preservation on rollback, and a genuinely failing drift guard.
Evidence: `.evidence/AXR1-006-fix-1.txt`, `-fix-2.txt`, `-fix-3.txt`, `-fix-4.txt`,
`-fix-5.txt`, `-failure.txt`, `-placement-rebase.txt`, `-placement-collection.txt`,
`-placement-related.txt`, `-placement-migration.txt`, and `-placement-gates.txt`.
The populated rollback/reapply retained obligations, attached documents and both
audit streams. `AXR1-007` (in-product onboarding behind `import_r1`: create/edit
with a tenant-scoped duplicate check and versions, engagement, tax-profile,
contact and assignment editors, contact portal invites issued under lock and
mailed after commit, and the CSV/XLSX portfolio import with plan, TenantTask
commit, retry and dependents-aware rollback; `ImportBatch`/`ImportRow`,
`clients.0015`) is recorded on the T6 stack, using the S8.1 `clients.manage` and
`clients.import` capabilities and keeping the MEI flag in agreement with the tax
timeline on every new write path. Tests: `tests/clients/test_import_wizard.py`,
`tests/clients/test_onboarding_screens.py`,
`tests/clients/test_contact_invite_delivery.py`,
`tests/clients/test_onboarding_flag_agreement.py` and
`tests/browser/test_onboarding.py`. Evidence: `.evidence/AXR1-007-red.txt`,
`-happy.txt`, `-failure.txt`, `-adversarial.txt`, `-migration-reversal.txt`,
`-gates.txt`, `-fix-1.txt` through `-fix-4.txt`, `-fix-6.txt`, `-fix-gates.txt`,
`-fix6-gates.txt`, `-placement.txt`, `-placement-migration.txt`,
`-placement-gates.txt` and the eight onboarding/import screenshots. `AXR1-008`
(versioned, typed playbooks behind `cycles_r1`: `Playbook`,
`PlaybookVersion`, `PlaybookStep` with draft-only mutation,
`expected_version` revision checks, persisted-parent reparent guards and a
preview-signed publish; the MEI mensal and Coleta mensal system packs installed
per-tenant under a signal, converging under concurrent install; targeting
driven by `TaxProfile`/`Engagement` snapshots, never the legacy flag; gated on
`playbooks.manage`; `cycles.0001`) is recorded on the T7 stack. Tests:
`tests/cycles/test_playbook_versioning.py`, `test_adversarial.py`,
`test_playbook_http_conflicts.py`, `test_step_formset.py` and
`tests/browser/test_playbook_editor.py` plus `test_playbook_publication.py`.
Evidence: `.evidence/AXR1-008-red.txt`, `-happy.txt`, `-failure.txt`,
`-adversarial.txt`, `-gates.txt`, `-fix-1.txt` through `-fix-4.txt`,
`-fix2-1.txt` through `-fix2-3.txt`, `-fix2-mutations.txt`, `-fix2-repeat.txt`,
`-fix2-review.txt`, `-fix2-adversarial.txt`, `-fix2-gates.txt`,
`-placement.txt` and the editor screenshots. This verified a populated-schema
round trip: `migrate cycles zero` dropped the playbook tables and their seeded
rows; forward migration recreated the schema. It did not preserve those rows.
`AXR1-009` (service cycles, pinned task graphs, private notes and exception
openers, append-only timeline, monthly generation, client workspace and the
restricted portal projection; `cycles.0002`) is recorded on final T8, using the
live S8.1 cycle capabilities and the shared portfolio loader. Gate repairs
preserve flag-off portal bytes against captured T8 parent/home templates,
return an existing authorized cycle after definition retirement or scope
changes without rewriting its pinned graph, and identify the client by name
and CNPJ alongside engagement, period and state on the write surface.
Tests: `tests/cycles/test_cycle*.py`,
`tests/clients/test_import_rollback_with_cycles.py`,
`tests/isolation/test_portal_cycles_isolation.py`,
`tests/portal/test_cycle_flag_off.py`, `tests/browser/test_cycle_workspace.py`
and `test_cycle_context.py`. Evidence: `.evidence/AXR1-009-fix-0.txt` through
`-fix-3.txt`, `-fixes-repeat.txt`, `-placement-baseline.txt`,
`-placement-migration.txt`, `-placement-gates.txt` and both-viewport screenshots.
The populated T9 schema round trip (`migrate cycles 0001`, then forward) dropped
and recreated the six new tables empty; six pre-existing audit events, one
source observation and their shared append-only protections survived. This is
schema reversibility, not restoration of the deleted cycle rows.
`AXR1-010` (the durable delivery outbox behind `cycles_r1`: `PhasedTenantTask`,
SKIP LOCKED claims committed before SMTP I/O and fence-checked finalization,
append-only attempt and effect receipts, the independent `effects-journal/` and
`reconcile_effects`, contact-only recipients, the pre-send hook registry, global
`DELIVERY_PAUSED` (default on) plus a firm pause gated on `automations.manage`,
the daily attempt cap, and the `/comunicacao/` queue; `delivery.0001`) is
recorded on final T9. An ambiguous SMTP outcome is `resultado_incerto`, never
retried, and opens a cycle exception; nothing is labelled delivered. Tests:
`tests/delivery/`, `tests/core/test_phased_tenant_task.py`,
`tests/security/test_outbox_log_hygiene.py` and
`tests/browser/test_delivery_queue.py`. Evidence: `.evidence/AXR1-010-red.txt`,
`-happy.txt`, `-failure.txt`, `-adversarial.txt`, `-migration.txt`,
`-gates.txt`, `-fix-1.txt`, `-fix-gates.txt` and both-viewport screenshots.
Operator gates OG-03 (a real send from the production host to an owner mailbox)
and OG-04 (retention and restore isolation of the journal prefix) remain
**open**; sending stays paused until both are recorded. Todos 11 to 29 and the
final verification wave remain open.

`AXR1-011` (the client portal's request inbox, upload against a request, the
company switcher and firm branding, all behind `cycles_r1`; `cycles.0003`,
`delivery.0002`, `tenants.0009`) is recorded on the `AXR1-010` outbox branch; like that row, it
describes code that is not yet on `main`. Client-visible `EvidenceRequest` and firm-only
`RequestInternal` are separate tables (S8.2): the portal role reads requests,
links and intakes under the RESTRICTIVE client policy and holds INSERT on
`cycles_evidencelink`, `cycles_documentintake` and `cycles_cycleevent` — the last
without SELECT, because the timeline also carries firm-side events. An upload
inserts rows only; "Enviado pelo cliente" and "Recebido" are derived from the
newest link, never stored, and neither takes the verified style. The same bytes
twice produce one `Document` and a second link, over 10 MiB is a pt-BR 400, and a
foreign request id is 404. The company switcher only selects among memberships
re-verified on every request, rotates the session key, and answers 403 with no
change for a non-member. Portal invitations leave through the outbox, with the
token minted at dispatch and the previous link answering 410; no raw token is
written to any row, attempt or log. Branding validates the colour against the two
product foregrounds — at least one must reach AA, and the portal renders the one
that does — and accepts a PNG of at most 200 KB, served through the app.
Carried forward, not closed: a queued portal invitation whose message the
provider rejects stays live, and the firm's "Cancelar convite pendente" control
is the recovery until a terminal-state withdrawal lands with T13/T16. Interim
until `AXR1-012`: a new upload is stored with an intake row in state `recebido`
and re-served only to its uploader, and `AXR1-012` replaces that rule with
quarantine and scanning. `OG-03` (a real production SMTP send) and `OG-05`
(malware scanning) are OPEN and neither is claimed by this work; sending stays
paused (`DELIVERY_PAUSED` defaults to true), so the only SMTP traffic in the
evidence is the tests' own fake peer. Tests:
`tests/portal/test_requests_inbox.py`, `test_upload_against_request.py` and
`test_recipient_without_seat.py`, `tests/cycles/test_duplicate_upload_relink.py`
and `test_request_services.py`, `tests/delivery/test_portal_invite_outbox.py` and
`test_reminder_reevaluation.py`, `tests/isolation/test_multi_company_switch.py`,
`tests/tenants/test_branding.py` and
`tests/browser/test_journey_portal_upload.py`. Evidence:
`.evidence/AXR1-011-baseline.txt`, `-red.txt`, `-happy.txt`, `-failure.txt`,
`-adversarial.txt`, `-migrations.txt`, `-gates.txt` and the five 390x844 portal
screenshots plus the 1440x900 staff one. The populated T11 schema round trip
reversed `delivery.0002`, `cycles.0003` and `tenants.0009` and re-applied them;
it dropped the four new tables with their seeded rows and left the pre-existing
outbox, document and append-only protections intact.

`AXR1-012` (quarantined document intake behind `cycles_r1`; `cycles.0004`) is
recorded on the `AXR1-011` branch. Every portal upload is written under
`quarantine/{tenant}/{client}/`, type-sniffed by magic bytes at offset 0 (PDF, XML,
PNG, JPEG only; declared type and extension ignored; polyglots, SVG, ZIP, HTML and
Office refused), scanned through the `Scanner` port (`ClamdScanner` over INSTREAM
with timeouts, `FakeScanner` for tests) in a phased task that streams outside any
transaction, and moved to its final key only after a clean verdict. Until then the
portal answers 404 and staff 409; malware is `rejeitado_malware`, never linked or
served, with the original retained. Retries back off to a 24 h deadline, then
`verificacao_indisponivel` opens one exception. XML is parsed only through
`defusedxml` and PDF text through `pypdf` (50 pages, 10 s checked between pages);
served files carry `Content-Security-Policy: sandbox`, `nosniff` and an RFC 5987
filename. This replaces the `AXR1-011` interim rule. `CLAMAV_ENABLED` defaults to
false, so until the gates below close, client uploads are downloadable by staff only
behind a warning and never re-served to the portal; the `clamav` sidecar is
digest-pinned behind a compose profile and was never started. Dependencies
`filetype`, `defusedxml` and `pypdf` are proposed. Tests:
`tests/cycles/test_intake_hardening.py` and
`tests/security/test_document_preview_headers.py`. Evidence:
`.evidence/AXR1-012-baseline.txt`, `-red.txt`, `-happy.txt`, `-failure.txt`
(`defusedxml` swapped for `xml.etree` fails the XXE cases), `-gates.txt`,
`-adversarial.txt`, `-mutations.txt` and `-migrations.txt`. OG-05 (host memory
and freshclam on Lightsail), OD-11 (host upsize) and OG-04 (quarantine prefix
lifecycle) remain **open**. Carried to `AXR1-013`: the legacy portal vault upload
still bypasses this pipeline, and the Celery soft limit that bounds one slow PDF
page. Todos 13 to 29 and the final verification wave remain open.

`AXR1-013` (deterministic document extraction, field provenance, wrong-company
quarantine and staff review; `cycles.0005`, `obligations.0019`) is recorded on
the final `AXR1-012` branch, not on `main`. The scoped `/documentos/` queue and
review route expose XML/PDF suggestions and their origin without modifying
original bytes. Unknown XML layouts and images make no automatic verification
claim. Accepted corrections stay in `Review`; rejected files retain a pt-BR
reason visible to their client. Capability, portfolio scope and rendered
version are checked under the actual cycle/request locks; stale writes answer
409 without effects. Wrong-company findings suspend links, preserve ownership
and propagate their exception to later cycles when the same bytes are linked
again. The legacy vault upload path now enters intake, and historical
unverified client uploads fail closed when served. The PDF worker's soft/hard
bounds are 15/30 seconds; interruption handling does not claim a timed
live-worker kill.

The review table contains its hidden labels at 390 pixels while retaining
accessible associations and table-only horizontal scrolling. Keyboard
decisions wait on navigation-load signals, not timing sleeps. An independent
review confirmed `8f5f475`, closed both review findings, reproduced six
XML/PDF viewport cases, and proved that removing the containment class fails
the mobile journey at 628 versus 390 pixels. Both source hashes were restored
and both keyboard journeys passed again. Native headless PDF rendering is
unverified; actual clicks returned the unchanged original bytes and safe
headers. The populated schema reversal preserved pre-existing documents,
links and shared protections; it dropped the added review/extraction rows,
which forward migration does not restore.

Tests: `tests/cycles/test_extraction.py`, `test_wrong_company_upload.py`,
`test_review_concurrency.py`, `test_duplicate_upload_relink.py` and
`tests/browser/test_document_review.py`. Evidence:
`.evidence/AXR1-013-reviewer-final-attacks.txt`, `-reviewer-remaining.txt`,
`-ui-fix-red.txt`, `-ui-fix-render.txt`, `-ui-fix-gates.txt`,
`-rv-8f5f475-baseline.txt`, `-rv-8f5f475-mutation.txt`,
`-rv-8f5f475-restored.txt` and `-rv-8f5f475-cleanup-final.txt`.
All owned review containers, volumes, processes and temporary paths were
removed. Existing semantic-template diagnostics and a favicon 404 are
documented, not suppressed. OG-03/OG-04/OG-05 and OD-11 remain **open**;
no production email, scanner activation or pilot authorization is claimed.
Search and AI-shadow exclusion proofs remain in `AXR1-021`/`AXR1-024`.

`AXR1-014` (owned exceptions, deterministic openers, policy-restricted risk
acceptance and client-safe help; `cycles.0006` and `cycles.0007`) is recorded on
the final `AXR1-013` branch. Staff can assign, triage and resolve from the cycle
or client workspace. Scoped evidence links reach the actual document, message
or current subject; reload, Back and close preserve the selected drawer and
natural keyboard focus. Internal instructions and references live in the
portal-denied `ExceptionInternal` companion, not the portal-readable row.
Populated reversal/reapply preserved stored private values, aggregate identities,
T13 data, grants and shared protections.

The rejected portal-invitation finalizer withdraws the invitation and retains
its rejected delivery and audit record. Its exception belongs to the newest
applicable open cycle; with none, no cycle or unrelated exception is fabricated.
Replay does not resend or duplicate the terminal records. A further independent
portfolio-revocation probe found a stale authorization lookup; the locked cycle
loader now rechecks visibility before writes, with a real committed
second-connection regression.

The independent gate confirmed code `c956af9` after closing privacy, navigation
and revocation findings. Tests include `tests/cycles/test_exceptions.py`,
`test_exception_privacy.py`, `test_exception_policy_boundaries.py`,
`test_exception_evidence.py`, `test_exception_navigation.py`,
`test_exception_subjects.py` and `tests/browser/test_exceptions.py`. Evidence:
`.evidence/AXR1-014-rv-final.txt`, `-rv-adversarial.txt`, `-rv-mutants.txt`,
`-rv-restored.txt`, `-rv-revoke-red-final.txt`, `-rv-revoke-green.txt`,
`-rv-revoke-typing.txt` and the final natural/terminal browser captures.
Thirty-six natural evidence journeys covered nine reason families on both staff
surfaces at both widths; actual portal SQL and rendered privacy controls passed.
All owned resources were removed. Existing portal placeholder copy and four
JavaScript diagnostics remain documented; live operator and owner gates remain
**open**. No production delivery or pilot authorization is claimed.

`AXR1-015` (immutable exact-effect proposals and append-only approval decisions;
`cycles.0008`, `authz.0007`, `tenants.0010`) is recorded on the final
`AXR1-014` branch. The closed manifest uses canonical UTF-8 bytes and a SHA-256
digest to bind selected recipients, documents, evidence, source facts, money,
policy and authority. Propose, approve, reject, revalidate and cancel are scoped,
versioned services. Material changes invalidate the old revision without
rewriting its bytes or decisions; unrelated facts follow the action-specific
invalidate/warn/ignore matrix.

Email proposals freeze real CRLF MIME bytes, headers and their hash, using
persisted proposal facts rather than the render clock. Relevant portfolio
assignment identities bind both initiator and approver authority: removing
and re-adding an assignment cannot revive a decision. Firm-wide and unrelated
assignments do not cause blanket invalidation. Self-approval is refused by
default; the sole-staff exception requires owner policy, audited acknowledgment
and explicit acknowledgment for each decision.

The staff card shows the frozen effects before decisions, including
keyboard-accessible mail headers. Approval records only a decision: it does
not send mail, publish files or prove execution. Executor integration remains
`AXR1-016`. The independent gate confirmed `6089575` after closing both MIME
and portfolio-history findings. Populated reversal/reapply preserved inherited
T14 private values, T13 data and shared protections; it intentionally removes
the new proposal/approval rows.

Tests include `tests/cycles/test_canonical_digest_vectors.py`,
`test_effect_manifest.py`, `test_approval_invalidation.py`,
`test_effect_sources.py`, `test_revalidate_revocation.py`,
`test_approval_storage.py`, `test_approval_authority_races.py` and
`tests/browser/test_approval_card.py`. Evidence:
`.evidence/AXR1-015-rv-final-std.txt`, `-rv-final-probes.txt`,
`-rv-final-restoration-cleanup.txt` and six fresh desktop/mobile card captures.
Ten independent semantic cases passed; the updated recipient mutation caused
three behavioral failures, followed by three restored passes. All owned
resources were removed. Nonblocking reason-formatting, UUID recipient labels
and inherited default error pages remain documented; owner/operator gates
remain **open**. No production effect or pilot authorization is claimed.

`AXR1-016` (approved client publications, email notices and acknowledgements;
`cycles.0009`) is recorded on the final `AXR1-015` branch. The four
consequential executors (publication, email notice, cycle completion and
request waiver) rebuild live facts and rerun exact-effect revalidation before
their first write, and authorization holds through the durable dispatch-start
write; SMTP I/O happens outside the transaction through the existing outbox
gateway, one envelope recipient per message, and an unknown outcome is never
resent. `ClientPublication` is portal-readable; `ClientAcknowledgement` is
portal insert-only and append-only and binds the exact publication digest. An
authenticated portal GET inserts a viewed event (HEAD, prefetch and anonymous
requests do not), and the explicit receipt is independent of the SMTP outcome.
The firm queue shows provider outcome and receipt separately and never claims
"entregue" or "lida". Withdrawal needs a new approved proposal; the waiver and
completion forms now propose instead of bypassing approval. Ordinary completion
still requires the inherited evidence-index guard owned by `AXR1-018`.

The independent gate confirmed `75272c6`: both revocation/dispatch orderings
against a real SMTP peer, a biting revalidation-removal mutation, populated
reverse/forward preserving 20 inherited tables, and inspected desktop/mobile
keyboard journeys. Tests include `tests/cycles/test_publication_flow.py`,
`test_approval_revocation.py`, `test_dispatch_race.py`,
`test_ack_independent_of_smtp.py`, `tests/isolation/test_publication_scope.py`
and `tests/browser/test_publication_ack.py`. Evidence:
`.evidence/AXR1-016-gates.txt`, `-failure.txt`, `-adversarial.txt` and the
reviewer's `-rv-*` artifacts. OG-03, OG-04 and every owner/operator gate remain
**open**; only a local fake SMTP peer was used.

`AXR1-017` (source observations and the honest coverage panel; `cycles.0010`,
`obligations.0020`) is recorded on the final `AXR1-016` branch. The obligation
calendar writes `calendar_scheduled` assertions per client month, staff record,
correct and retract "guia emitida" and "pagamento informado" facts against a
locked subject chain, and a `comprovante` request binds its obligation only
while it is a draft and only inside the engagement's half-open date window. A
submitted receipt stays `pending_review` until a clean review accepts it, and it
is then labelled manual. `coverage_for(client, period)` answers each in-scope
obligation with `covered_by`, `last_success` and one of unknown, not_applicable,
manual, pending_review, stale or refresh_failed. The panels in the client
workspace and the cycle render these states verbatim, and nothing manual or
unknown renders green or "confirmado". `coverage_for_clients()` answers the same
question for an already scoped set of clients with four bound queries, and
`AXR1-019` uses it. R1 produces no official source.

The first independent review rejected the original head with seven blocker
groups. The repair (`71fd6b8`) closed them, and a second independent review
reproduced every original attack as passing. It found one more gap (receipt
requests ignored engagement dates), which `1902cf7` closes. A third independent
review confirmed that fix: eight real HTTP scenarios, a biting mutation and its
restoration, historical reads, and the complete serial gates at `1902cf7`.
Tests include `tests/obligations/test_coverage.py`, `test_coverage_repair.py`,
`test_bulk_coverage.py`, `test_receipt_dispatch.py`,
`tests/cycles/test_payment_evidence_labels.py`, `test_receipt_repair.py`,
`test_observation_locks.py`, the extended `tests/ui/test_status_render_policy.py`
and `tests/browser/test_coverage_panels.py`. Evidence:
`.evidence/AXR1-017-repair-gates.txt`, `-repair-failure.txt`, `-n1-red.txt`,
`-n1-full-gates.txt` and the reviewers' `t17-rv2` and `t17-rv3` artifacts. Every
owner/operator gate remains **open**.

`AXR1-018` (justified completion, amendments, evidence index, bundle export and
retention classes; `cycles.0011`-`0015`, `delivery.0003`, `obligations.0021`) is
recorded on the final `AXR1-017` branch. Completion has one authority: the
cycle page shows a linked checklist of request, exact-effect and evidence-index
guards, and "encerrado com pendências" needs a reason for each item that is
unmet at that moment. The reason is keyed by guard, subject and revision, and
the set is recomputed under the cycle lock. Amendments never rewrite the frozen
completion generation, and reopening needs `cycles.reopen` and a separate
approver. The evidence index validates against
`apps/cycles/schemas/evidence_index_v1.json`. The bundle export is a phased
tenant task: a committed `building` claim, storage I/O outside any transaction
with byte caps on actual bytes, and a fenced finalize. Download goes through
the app only, for the firm only, and the bundle is never labelled a
certificate, an official audit or tamper-proof. Retention classes, legal holds
and `purge_expired_content` (dry-run by default, counts only) cover documents,
links, messages and AI-derived content, and `docs/retention.md` lists the
streams.

The independent gate review confirmed `cae2ba6` (tree `5c16466`) with zero
blockers. All eleven attacks passed, as did real-role probes of the narrow
SECURITY DEFINER retention trigger: fixed search_path, revoked EXECUTE, RLS and
hostile-shadow attempts. Seven populated migration round trips preserved rows
and grants. The guard-test audit covered 29 registrations and six existing-test
edits. Removing the open-exception guard made the completion tests fail, and
restoring it passed them. A real 101,355-byte bundle download matched its hash
and manifest. Evidence: `.evidence/AXR1-018-happy.txt`, `-failure.txt`,
`-gates-placed.txt` and the reviewer's `-rv-*` artifacts. OG-04, OG-08 and every
owner/operator gate remain **open**; purge stays dry-run.

`AXR1-019` ("Hoje": morning briefing and explainable exception queue;
`cycles.0016`) is recorded on the final `AXR1-018` branch. For tenants with
`briefing_r1`, providers for obligations, overdue requests, exceptions,
approvals waiting, delivery outcomes, divergent documents and stale coverage
each yield a `BriefingItem`, ranked by `BRIEFING_RULES_V1` factors with a stable
tie-break and keyset pagination. Items and counts come only from
`visible_clients`. Saved views are re-authorized at render. A "Não relevante"
dismissal needs a reason, applies only to the acting user, and expires when the
underlying fact changes. The page shows "Calculado às HH:MM", "Mudanças desde
ontem", loading and stale-view states, and a 403 drops stale counts. Tenants
without the flag keep the old dashboard byte-identical. With 1,000 clients, the
whole Hoje request takes exactly **25** statements, under the plan's
`django_assert_max_num_queries(25)`. The repaired base started at 8,032. No
authorization answer is cached or shared. Eight existing exact query-count
registrations went down because their counts really fell; none went up.

The independent gate review found two blockers at `0fc87e3`: one stale
coverage item raised the 1,000-client Hoje request to 26 statements, and a
maximum-length client name widened the page sideways. Repair `3c8e5e2` reuses
the engagement batch the coverage reader already loads and wraps the client
line; a scale test in which every provider has items, stale and
refresh-failed coverage included, failed at 26 and now stays within 25, and a
long-name browser test failed and now passes at both viewports. No query-count
registration changed. An independent confirmation review of the unchanged
`3c8e5e2` returned **confirmed** with zero blockers: richer 1,000-client
requests each used exactly 25 statements, hostile and maximum-length names fit
both viewports, and removing the fix makes the new scale test reject 26
statements. Evidence:
`.evidence/AXR1-019-happy.txt`, `-failure.txt`, `-gates-placed.txt`,
`-adversarial.txt`, `-fix-red.txt`, `-fix-red-overflow.txt`, `-fix-green.txt`,
`-fix-green-overflow.txt`, `-fix-gates.txt` and the reviewers' `T19-RV-*` and
`T19-RV2-*` artifacts. Every owner/operator gate remains **open**.

`AXR1-020` (bulk operations with an eligibility preview, per-item results,
cancel and resume; `cycles.0017`) is recorded on the final `AXR1-019` branch.
For tenants with `bulk_r1`, a `BulkOperation` freezes its preview as a
snapshot with a digest and an expiry, and execution uses only that snapshot.
An expired or changed preview is refused, and confirming requires typing the
exact item count. Each `BulkItem` is unique per tenant, client, operation and
subject. That key is also the runner's claim and idempotency key. The runner
processes each item in its own tenant transaction, with a fenced claim and
per-item capability and version checks. A changed record is skipped as
changed, cancel stops before the next claim, and resume after a crash creates
no duplicate effect. Bulk approval delegates every item to the `AXR1-015`
per-item digest and revalidation. The results page polls and stops at a
terminal state. It says "Concluído com falhas: N" whenever an item failed, and
its CSV export is formula-safe. Published playbook versions are labelled with
the playbook name and version number, and the preview shows how much of the
daily quota remains.

The independent gate review found one blocker at `da8da54`: notice previews
did not show the remaining daily quota the plan requires (with a cap of 100
and 97 sends reserved, the preview omitted the 3 left). Repair `1bec524` read
the same `remaining_quota()` helper the Comunicação queue uses, but stored the
value when the preview was created; its confirmation review found that
reloading a preview after reservations rose to 100 still showed 3 while the
queue showed 0. Repair `549d202` reads the remaining quota live on every
render of the preview (one read per render, none per item and none on the
progress poll), says when the previewed notices exceed it and that the excess
waits in the queue until reservations renew, and removes the stored value.
Previewing reserves nothing; execution still uses the frozen item snapshot
and the send-time quota. Tests failed before each repair and pass after it,
and rendering a stale or capped value makes them fail again.
An independent confirmation of repair 2 returned **confirmed**
with zero blockers: the same preview showed 3 at 97/100, stayed 3 after
another firm's reservations and reloaded to 0 after its own; a confirmed run
used the frozen digest, version and audience, queued one notice and made no
send attempt; and rendering the stale value makes the HTTP test fail. Evidence:
`.evidence/AXR1-020-happy-placed.txt`, `-failure.txt`, `-rv-quota.md`,
`-fix-red.txt`, `-fix-green.txt`, `-fix-mutation.txt`, `-fix2-red.txt`,
`-fix2-green.txt`, `-fix2-mutation.txt`, `-fix2-gates.txt` and the reviewers'
`T20-RV*` artifacts. Every owner/operator gate remains
**open**.

`AXR1-021` (permission-scoped search and command palette; `core.0001`) is
recorded on the final `AXR1-020` branch. For tenants with `cycles_r1` (the R1 product shell; search answers 404
otherwise),
`search(actor, q, kinds)` reads only querysets scoped by `visible_clients`,
needs at least two characters and returns at most five results per kind
without revealing counts. Documents are found by filename only, never by their
contents. Standalone and vault documents are included and open the page the
actor is allowed to use, and a document whose extracted CNPJ diverges never
surfaces. Matching is accent-insensitive through `unaccent` and `pg_trgm`, with
trigram GIN indexes on client names, the normalized CNPJ and request labels;
the migration asserts that both extensions exist and does not create them.
Full-width wildcards match only themselves. A header search field and a
`Ctrl/Cmd+K` palette render server results as a keyboard-navigable listbox,
no tenant value is interpolated into `x-*` or `hx-*` attributes, and the
portal has no global search in R1. Long filenames, client names and labels
wrap at phone width. Tailwind no longer scans the frozen legacy-dashboard test
fixture; the only stylesheet change from that is the removal of the unused
`.sm:inline` rule.

The pre-placement adversarial review found standalone documents unsearchable
and full-width wildcards unescaped; both were fixed with regression tests that
failed first. The independent gate review at `5235bea` returned **confirmed** with zero
blockers after attacking scope, runtime, the migration, mutations and keyboard
and browser behaviour; its own serial gates passed (3916 default, isolation
288, CNPJ 29, browser 78). It also noted that a permitted long, unbroken filename did not reflow
at 390 px (palette list 2507 px wide inside 318 px; `/busca/` 2540 px). Repair
`51afd08` makes names wrap in both renderings, and a 390/320 px browser test
failed before it and passes after. The confirmation review of the unchanged
`51afd08` returned **confirmed** with zero blockers: long names wrapped at every
tested width, with full accessible names and keyboard selection intact, and the
pre-repair layout failed its four new tests. Evidence: `.evidence/AXR1-021-failure.txt`,
`-static-drift.txt`, `-gates-place2.txt`, `-fix-red.txt`, `-fix-green.txt`,
`-fix-visual.md`, `-fix-gates.txt` and the reviewers' `t21-rv-*` and
`T21-RV2-*` artifacts. Every owner/operator gate remains **open**.

Execution of the full plan resumed on 2026-10-05 at the owner's instruction.
Todos 22 to 29 and the final verification wave remain open.

`AXR1-004` gate repairs (`2f4cf92`) add a locked membership grant revision and
409 conflict feedback, restore the original owner query budgets, and mirror the
portal NONE decision. Reversing the R1 seed removes only R1 grants (active and
revoked), retaining audit history and non-R1 data; reapplying does not restore
removed authority. Evidence: `.evidence/AXR1-004-fix-1.txt` through `-fix-5.txt`,
`-fix-repeat.txt`, `-fix-mutations.txt`, `-fix-browser.txt`, and `-fix-gates.txt`.

## Plan index

| Plan | Status | Basis |
| --- | --- | --- |
| `axorim-pivot-r1` | **current** (product direction) | Started 2026-09-27. 29 todos in five waves plus a final verification wave; `AXR1-001` recorded the pivot. It runs separately from the open F1-F4 wave and pilot authorization of `pilot-readiness-v2`. |
| `pilot-readiness-v2` | **current** | Execution complete: all 41 rows closed and `ops/check_go_no_go.py` exits 0 against the evidence directory, every S-line GO on the final capture. Only the F1-F4 audits and the owner's authorization remain. |
| `pilot-readiness-v1` | **superseded** | Replaced by `pilot-readiness-v2` on 2026-09-21. Its checkbox states are frozen and its evidence is kept, never rewritten; v2's legacy reconciliation ledger classifies every v1 row (13 verified-complete, 5 repo-complete with operator evidence open, 12 partial, 8 open-valid, 2 superseded, 2 invalid-premise, plus the four F-rows that never ran). |
| `resend-smtp-pilot-provider-amendment` | **superseded** | Superseded by owner decision D-Q1: Resend is used through the existing provider-neutral SMTP settings, so the amendment's new email machinery is unneeded. Its branch `chore/pilot-resend-smtp-round3h` is frozen, unpushed, and never to be merged. |
| `post-ui-ux-residual-hardening` | **closed** | All 24 todos checked. Its four pull requests merged; `main` is byte-identical to the final reviewed head. Its unfixed leftovers are written up in `docs/residual-risks.md`. |
| `ui-ux-post-completion-followups` | **closed** | All 11 todos checked. Seven findings that survived the UI/UX plan, closed as separate work. |
| `ui-ux-development-plan` | **closed** | All 39 todos checked; approved at a recorded head with the final verification wave accepted twice. The follow-ups plan above treats it as immutable and re-verifies its hash. |
| `fix-audit-failures` | **closed** | All 17 todos checked. It exists to make the failed success criteria of `accounting-mei-saas` pass with fresh evidence, and it names each one it repaired. |
| `phase-2b-todos` | **closed** | The work order decomposed from `phase-2b-portal-write`; its own status table marks stages 1 and 2 DONE and stages 3-5 are shipped in the tree (portal write access and the document vault are live). |
| `phase-2b-portal-write` | **closed** | Revision 9. All four of its open questions are marked CLOSED in the plan text; execution moved to `phase-2b-todos`. |
| `phase-2a-portal-isolation` | **closed** | Wave 1 marked CLOSED and Wave 3 marked implemented, code-reviewed, patched and deployed in the plan header. |
| `accounting-mei-saas` | **historical, partly superseded** | The original phase 0-1 build plan. Two rows of its Must-NOT-Have table are marked SUPERSEDED in place, pointing at the phase-2a and phase-2b plans. Its outstanding success criteria were closed by `fix-audit-failures`, not by itself. It is **not** the active plan, despite what older copies of the README said. |

These statuses were assigned by reading each plan's own checkboxes and status
headers. None is inferred from a filename.

### `open (needs …)` is a real state, and the plan ran on it

While `pilot-readiness-v2` was in flight, most of its rows were neither blocked nor
done. The plan
splits them by who can close them: `[E]` rows an agent can deliver, `[O]` rows that
need a real host, a real mailbox, a real bucket, or a real account — and the two
land at different times. An operator row reads `open (needs <account|host|mailbox|
bucket|window>)` until its evidence artifact exists; it is never `blocked`, and an
engineering row that only prepares a gate is never blocked by the gate being open.

Nothing in this repository closes an operator gate. A workflow that would assert a live
deploy is not a live deploy, a script that would copy to a bucket is not a copy, and a
runbook that documents a restore is not a rehearsal.

## How changes reach `main`

**Sequential pull requests, each based on current `main`.** Never stacked.

The reason is mechanical rather than stylistic. Continuous integration is configured
to run on pull requests whose base is `main`. A pull request stacked on another
feature branch therefore receives no CI run at all, and with required checks in place
it can never merge. Rebasing a stack onto `main` to unblock it re-runs everything
anyway, so the stack buys nothing. The accepted consequence is that a wave's work
waits for the previous wave's pull request to merge before it opens.

## Deploy cutover state

The push deploy in `ci.yml` (`deploy-pilot`) now targets the Lightsail pilot host
through the OIDC just-in-time firewall; `deploy-lightsail.yml` is slimmed to the
OIDC claim probe and the stale-rule cleanup, and the OCI-era `DEPLOY_*` secrets are
retired. Both states that were expected after the cutover merge have played out:

- **The shadow deploy happened.** The first push to `main` after the merge deployed
  `c7afe36` to Lightsail. The first attempt failed assert 4 because no certificate had
  been issued yet (the Cloudflare token was IP-restricted); after the token fix, the
  re-run passed all six asserts and the storage probe (`.evidence/PILOT2-403-happy.txt`).
- **`verify-live` is green.** It was red while the public edge still served the old
  OCI release. DNS flipped to Lightsail on 2026-09-22, and the scheduled run of
  2026-09-23 passed against the target (`.evidence/PILOT2-406-operator.txt`).

## Cutover record

last_premerge_sha=c63326d4a2372609a12b7d34cfacd9f46fc7adc1
window_utc=2026-09-23T14:00:00Z
rollback_deadline=2026-09-23T18:00:00Z
rollback_trigger=any step-7 check red OR any step-10 check red
rollback_semantics=rollback to the old host restores data as of freeze_utc; any write accepted on the target after the flip is lost; therefore NO pilot-firm access before row 34 closes
freeze_sha=aff146440f9f99f76df5a790146cbad8aada6174
freeze_sha_source=.evidence/PILOT2-404E-happy.txt
executed_window_utc=2026-09-22T13:15:00Z
executed_rollback_deadline_utc=2026-09-22T17:15:00Z
freeze_started_utc=2026-09-22T14:18:55Z
dns_flip_completed_no_later_than_utc=2026-09-22T14:36:59Z
rollback_invoked=no
cutover_status=closed
old_host_state=stopped-intact
old_host_termination_not_before=v0.2.0-rc1 tag date + 7 days (tag not yet created)

`freeze_sha` is the merged row-27 commit, backfilled from `.evidence/PILOT2-404E-happy.txt`
and repeated in `.evidence/PILOT2-404-operator.txt`. The `window_utc` and
`rollback_deadline` lines are the plan as first recorded. The owner then moved the
cutover forward a day, and the `executed_*` lines are what ran
(`.evidence/PILOT2-404-operator.txt`, `.evidence/PILOT2-404O2-operator.txt`). The OCI
host was stopped with its disk intact on 2026-09-23 (`.evidence/PILOT2-401-operator.txt`);
its termination date gets fixed once the `v0.2.0-rc1` tag exists.

The authoritative 12-step cutover table, with its as-executed record, is the ordered
state machine under "Cutover: data freeze, restore, and DNS flip" in `ops/README.md`.

## Suite baseline

The full test suite is the gate at every pull-request head and at the final
implementation head. The current default-suite baseline is **3918 passing**
(86 deselected), verified at placed `AXR1-021` code `d8963b8` (tree `a294f13`) on
`AXR1-020` repaired code `549d202`: the **3889** below plus **29** net-new
default-suite cases. The same serial run passed isolation (**288**), CNPJ
(**29**), browser (**86**), lint, format, mypy, migration drift and a
clean rebuilt-static check. The confirmation reviewer audited the repair's
identical run at `51afd08` and reran the search and palette tests. Evidence:
`.evidence/AXR1-021-gates-place3.txt` and `.evidence/T21-RV2-confirm.md`.
This status-only commit does not change the reviewed code; it sits on the
`AXR1-020` status commit `3e88991`.

The previous default-suite baseline was **3889 passing**
(78 deselected), verified at `AXR1-020` repaired code `549d202` (tree
`f9ee6d4`), on `AXR1-019` repaired code `3c8e5e2`: the **3826** below plus
**63** net-new default-suite cases. The same serial run passed isolation
(**282**), CNPJ (**29**), browser (**78**), lint, format, mypy,
migration-drift and a clean rebuilt-static check. The confirmation
reviewer reran lint, format, mypy and migration drift, the bulk and delivery
modules (118), isolation (282), the bulk browser tests (4) and the static
rebuild, all green. Evidence: `.evidence/AXR1-020-fix2-gates.txt` and
`.evidence/T20-RV3-gates.txt`.

The previous default-suite baseline was **3826 passing**
(74 deselected), verified at `AXR1-019` repaired code `3c8e5e2` (tree
`e734580`), on final `AXR1-018` code `cae2ba6`: the **3721** below plus
**105** net-new default-suite cases. The same serial run passed isolation
(**276**), CNPJ (**29**), browser (**74**), lint, format, mypy,
migration-drift and a clean rebuilt-static check. The confirmation reviewer
audited that run and reran 500 targeted tests, isolation (**276**), the six
`AXR1-019` browser cases and the restored scale test. Evidence:
`.evidence/AXR1-019-fix-gates.txt` and `.evidence/T19-RV2-gates.txt`.

The previous default-suite baseline was **3721 passing**
(68 deselected), independently verified at `AXR1-018` code `cae2ba6` (tree
`5c16466`), on final `AXR1-017` code `1902cf7`: the **3558** below plus
**163** net-new default-suite cases. The same serial review passed isolation
(**270**), CNPJ (**29**), browser (**68**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Evidence: `.evidence/AXR1-018-rv-gates.txt`
and `.evidence/AXR1-018-gates-placed.txt`.

The previous default-suite baseline was **3558 passing**
(68 deselected), verified at `AXR1-017` code `1902cf7` (tree `146169b`), on
final `AXR1-016` (`a3a8b4b`): the **3438** below plus **120** net-new
default-suite cases. The same serial run passed isolation (**245**), CNPJ
(**29**), browser (**68**), lint, format, mypy, migration-drift and a clean
rebuilt-static check, and the independent confirmation review checked that
log. Evidence: `.evidence/AXR1-017-n1-full-gates.txt`.

The previous default-suite baseline was **3438 passing**
(60 deselected), independently verified at `AXR1-016` code `75272c6`
(tree `ab532c7`), on final `AXR1-015` (`3d519e7`): the **3372** below plus
**66** net-new default-suite cases. The same serial review passed isolation
(**245**), CNPJ (**29**), browser (**60**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Evidence: `.evidence/AXR1-016-rv-std.txt`.

The previous default-suite baseline was **3098 passing**
(50 deselected), independently verified at `AXR1-014` code `c956af9`
(tree `6c3fb90`), on final `AXR1-013` (`0382ddf`): the **3029** below plus
**69** net-new default-suite cases. The same serial review passed isolation
(**225**), CNPJ (**29**), browser (**50**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Four additional browser cases performed
**36** natural evidence journeys. Evidence: `.evidence/AXR1-014-rv-final.txt`;
independent mutations, populated migration and revocation proofs are retained
with that task's evidence. The final runner recorded gate and cleanup exit 0.
This status-only commit does not change the approved code.

The previous default-suite baseline was **3029 passing**
(46 deselected), independently verified at `AXR1-013` code `8f5f475`
(tree `7f93ff2`), on final `AXR1-012` (`8bf17e3`): the **2956** below plus
**73** net-new default-suite cases. The same serial review ran isolation
(**222**), CNPJ (**29**), browser (**46**), lint, format, mypy, migration-drift
and a clean rebuilt-static check. Evidence:
`.evidence/AXR1-013-rv-8f5f475-baseline.txt`; the separate biting mutation,
exact restore, two passing keyboard cases and teardown are recorded in
`-mutation.txt`, `-restored.txt` and `-cleanup-final.txt` with that prefix.
This status-only commit does not change the approved code.

The previous default-suite baseline was **2956 passing**
(44 deselected), verified by an independent serial full run of `AXR1-012` placed on
the final `AXR1-011` head (`4772761`): the **2895** of `AXR1-011` plus **61**
net-new `AXR1-012` cases. The same review ran the browser suite (**44**), isolation
(**216**), the CNPJ pack (**29**), `npm run build` with a clean `static/` diff and
`docker compose -f docker-compose.prod.yml config` with and without the `clamav`
profile. This status-only commit does not change the tested code.

The previous default-suite baseline was **2895 passing**
(44 deselected), verified by one serial full run of `AXR1-011` placed on the
final `AXR1-010` head `9aa5dbc` (code at `ce92e78`). It is the **2777** of
`AXR1-010` plus **118** net-new `AXR1-011` cases covering the requests inbox,
upload and relink, the portal-invite outbox, reminder re-evaluation, the company
switcher, branding and the portal journey. Before placement the same code measured
**2893** on the `AXR1-010` feature commit `e184bc4` (2775), in the same lane
(`.evidence/AXR1-011-baseline.txt` and `-gates.txt`). On the placed code the separate
browser run passed **44**, isolation **216**, the CNPJ pack **29**, and the delivery,
portal and cycle subset **497** (`.evidence/AXR1-011-placement-gates.txt`). This
status-only commit does not change the tested code.

The previous default-suite baseline was **2777 passing**
(42 deselected), verified by one serial full run on the `AXR1-010` code
(`54e76ad`). It is the **2669** collected on final T9 (`a521e50`: the 2667 below
plus two replay fast-path regressions) plus **108** net-new `AXR1-010` cases:
72 in the new delivery, phased-task and outbox-hygiene modules, and 36 from
existing table-enumerating meta-tests that now cover the four delivery tables.
The separate browser run passed **42**, isolation **180** and the CNPJ pack
**29**. Evidence: `.evidence/AXR1-010-fix-gates.txt` and `-fix-1.txt`. This
status-only commit does not change the tested code.

The previous default-suite baseline was **2667 passing**,
verified by one serial full run on the placed and repaired `AXR1-009` code
(`7d9db04`): the **2478** T8 baseline plus **189** net-new cases covering cycle
transitions, generation/replay, isolation and model inventories, import rollback
and flag-off compatibility. The separate browser run passed **40**, isolation
**168** and the CNPJ pack **29** (`.evidence/AXR1-009-placement-gates.txt`).

The previous default-suite baseline was **2478 passing**,
verified by one serial full run on the placed `AXR1-008` code (`6de1fc2`):
the **2407** below plus **71**
net-new `AXR1-008` tests (the versioned-playbook services, adversarial probes,
HTTP conflict matrix and formset regressions, plus the cycles-table portal
coverage cases). The browser run passed **36**, isolation **148** and the CNPJ
pack **29** (`.evidence/AXR1-008-placement.txt`).

The previous baseline was **2407 passing**,
verified by one serial full run on the placed `AXR1-007` code (`2a15498`): the
**2329** below plus **78**
net-new `AXR1-007` tests (the import wizard, onboarding screens, contact-invite
delivery and flag-agreement modules, including the gate-review regressions).
The browser run passed **30**, isolation **139** and the CNPJ pack **29**
(`.evidence/AXR1-007-placement-gates.txt`).

The previous baseline was **2329 passing**,
verified by one serial full run on the placed `AXR1-006` code head `8b968d0`.
Collection on T5 (`1222815`) found **2266** default tests; T6 adds **63** net-new
cases (33 from the reader/observation change and 30 gate-review regressions).
Both collections deselected the same 26 browser cases. The separate browser
run passed **26**, isolation passed **133**, and the CNPJ pack passed **29**.
Evidence: `.evidence/AXR1-006-placement-collection.txt` and
`.evidence/AXR1-006-placement-gates.txt`. The preceding T5 count was the **2228**
post-repair T4 baseline plus thirty-eight net-new tests from `AXR1-005`
(the client spine and its gate-review fixes).

The pre-repair baseline is **2213** passing, reproduced at `9995488` on 2026-09-27
(`.evidence/AXR1-004-fix-baseline.txt`). Originally recorded at `c428733` by
`AXR1-004`, it comprises the T3 baseline of 2100 plus one hundred
thirteen new tests: seventy-nine parametrized matrix cells, eleven positive
resolution tests, fifteen grant tests, six manager-wiring cases and three
RLS-coverage cases for `authz_usercapabilitygrant`, minus one replaced
cell-count sentinel. `AXR1-004` gate repairs (`2f4cf92`) carried it to **2228**
with fifteen gate-review regressions; committed-head gate evidence for that
repair lives in `.evidence/AXR1-004-fix-gates.txt`.

Browser tests are a separate suite. `uv run pytest` deselects them (`addopts` carries
`-m "not browser"`), so they are not part of the count above. They run with
`uv run pytest -m browser` after `uv run playwright install chromium`, and in CI in the
`browser` job. The suite now contains **86** cases: the **24** passing at the T3
commit `bff2bb2`, plus the two-viewport reviewer-conflict regression, plus the
four `AXR1-007` onboarding and import-wizard cases (two journeys at two
viewports), plus the six `AXR1-008` playbook cases (editor keyboard reorder and
preview-bound publication journeys at two viewports), plus the four `AXR1-009`
workspace/portal and distinct-client context cases (two journeys at two
viewports), plus the two `AXR1-010` Comunicação queue cases (one journey at two
viewports), plus the two `AXR1-011` portal-upload/staff-request cases, plus the
two `AXR1-013` document-review cases, plus the four `AXR1-014` exception cases,
plus the eight `AXR1-015` approval-card cases, plus the two `AXR1-016`
publication-receipt cases, plus the eight `AXR1-017` coverage-panel cases
(four journeys at two viewports), plus the six `AXR1-019` briefing cases
(two briefing-state journeys and one long-client-name journey at two
viewports), plus the four `AXR1-020` bulk-operation cases (the bulk journey
and the remaining-quota preview at two viewports), plus the eight `AXR1-021`
palette and search cases (the palette journeys at two viewports and the
long-name reflow checks at 390 and 320 px). Every test in
`tests/browser/` runs at 1440x900 and at 390x844.

The rule is about the delta, not a fixed number. A documentation-only change must
leave the latest recorded count unchanged. A change that adds tests raises it by
exactly the tests it adds, and that pull request records the new count here with its
delta. A count that drops without a recorded, classified reason is a failure.

History. The `pilot-readiness-v2` baseline was **2024** passing, a clean serial run on
merged `main` at `b19edf3` and later. Later pull requests added tests without updating
that figure, which is how it drifted to 2053 and why the rule now follows the latest
recorded count.

Counts recorded under `pilot-readiness-v1`, kept for provenance:

- `main`: **1912** passing.
- `chore/pilot-w0-governance`: **1918** passing (1912 plus two tests added by
  PILOT-006 and four by PILOT-007).
- `feat/pilot-w1-email`: **1946** passing.
- `feat/pilot-w2-readiness` at `b4e2cbc`: **1994** passing. The wave-2 delta is
  PILOT-201 (+14), PILOT-202 (+12), PILOT-203 (+2), PILOT-205 (+14), PILOT-206 (+5) and
  PILOT-207 (+1), which is 1946 + 48.

One consequence of the test setup is binding: there is a single shared PostgreSQL
test database, so test runs are strictly sequential. Two concurrent runs collide.

Documentation-only work must leave the count unchanged. Note that the guard in
`tests/scope/test_comment_references.py` scans `docs/` as well as the source roots,
so any file path or test module named in this directory has to resolve on disk.

## Durable records

| Record | What it holds |
| --- | --- |
| `docs/residual-risks.md` | Accepted risks and known gaps from the hardening work. |
| `docs/residual-risks-pilot.md` | The same, scoped to pilot readiness. |
| `docs/decisions/` | Architecture decision records. |
| `docs/product/pivot-supersession.md` | The Axorim pivot: superseded restrictions, constraints still in force, and R1's engineering scope. |
| `docs/product/vocabulary.md` | The pt-BR status vocabulary and the rules for success words. |
| `docs/lgpd.md`, `docs/retention.md` | Data-protection and retention posture. |
| `docs/regulatory-watch.md`, `docs/fiscal/` | Fiscal parameters and what to watch. |
| `docs/frontend.md` | Front-end conventions. |
| `ops/README.md`, `ops/RESTORE.md` | Operations and recovery procedures. |
| `ops/PILOT-GO-NO-GO.md` | The pilot go/no-go ledger: one row per `pilot-readiness-v2` Success-criteria line (S1-S21). `ops/check_go_no_go.py` checks it against the untracked evidence and the final capture; exit 0 is the precondition for the F-wave. |

## External deadlines

`pilot-readiness-v1` kept a checklist of seven dated checkpoints here, every one of
them a formula over two operator-supplied dates. `pilot-readiness-v2` row 2 resolved
them against the real consoles on 2026-09-21, and the resolved ledger below replaces
the formulas. No external clock still binds the schedule; the OCI trial mooted
itself.

| # | Checkpoint | Resolved state |
| --- | --- | --- |
| 1a | OCI trial expiry read from the billing console. | **Resolved.** The trial ended 2026-08-28 and the host survived on the always-free tier. The instance was stopped on 2026-09-23 with its volumes intact; its termination date is set by the `v0.2.0-rc1` tag decommission window, and no reclamation notice is pending. |
| 1b | OCI storage disposition decided. | **Resolved: keep-oci.** The OCI documents bucket stays the document store — versioning enabled, a 30-day previous-versions lifecycle rule, and the free storage entitlement persists. There is no migration to AWS; v2's conditional move row (15) is skipped by this ruling. |
| 2a, 2b | SES production access submitted, then reviewed. | **CLOSED-ABANDONED.** The appeal is closed by owner decision D-Q1: mail goes through Resend SMTP instead, and no SES checkpoint remains. |
| 3 | UptimeRobot capability confirmed. | **Resolved, as keyword substring.** UptimeRobot cannot evaluate JSONPath; its only body-inspection primitive is a substring match, so the gate is keyword capability, not JSON assertion. Four keyword monitors plus the impossible-keyword negative control are configured per `ops/README.md` "External uptime monitoring". |
| 4 | Healthchecks.io account and first check. | **Resolved.** The account exists and the backup dead-man checks are live with transitions proven; v2 adds target-scoped checks so the two hosts never share one. |
| 5a | VPS ordered. | **Resolved.** The Lightsail instance `app-mei-pilot-sa-east-1` (18.229.187.66) is provisioned and running, static IP attached, firewall open on 80/443 and operator SSH. |
| 5b | Base-host bootstrap, Phase A. | **Resolved.** Phase A ran on the Lightsail host (memory preflight passed, swap remediated) and the stack was then torn down ahead of Phase B, which is v2 row 16 and has since closed. |
| 6a, 6b | Binding go/no-go at `E−5`; cutover by `E−3`. | **Superseded.** `E` has passed with the host alive, so both formulas are moot. The cutover window, freeze, and rollback deadline are set by v2 row 27 instead. |
| 7 | Same-day escalation of any missed checkpoint. | **Standing.** Unchanged: a missed checkpoint reported late is two failures, not one. |

The Let's Encrypt certificate on the OCI host expires 2026-10-27. The cutover ran
on 2026-09-22, well inside that window, and the public name is now served by
Lightsail, where Caddy issues its own certificate. The date stays only as
provenance for this ledger.

### The UptimeRobot negative control

The capability proof is a monitor that must stay DOWN. It points at the same
`https://samaronefialho.dev/healthz` URL on the same interval with the same
alert-on-absence setting as the four real monitors, keyed on a keyword the endpoint
can never render — `scheduler` only ever renders `alive`, `stale` or `unknown`, so
the impossible-keyword string documented in `ops/README.md` cannot appear in any
body. Acceptance is four UP and one DOWN, and the DOWN one is required: if the
control ever reports UP, keyword evaluation is not happening and the four green
monitors are asserting nothing.

### Where each procedure lives

| Checkpoint | Procedure |
| --- | --- |
| 2a, 2b (mail) | `ops/README.md`, "Email provider (Resend SMTP)" — the SES paragraphs are kept at the end of that section as history. |
| 1b (OCI storage) | `ops/README.md`, "Object storage: versioning, lifecycle, probe". |
| 5a (VPS order) | `ops/README.md`, "Host requirements". |
| 5b (bootstrap) | `ops/README.md`, "New host bootstrap (two phases)". |
| 6b (cutover aftermath) | `ops/README.md`, "Decommissioning the old host", and "Host log rotation" for the new host's retention. |
| 3, 4 (monitoring) | `ops/PILOT-RUNBOOK.md`, [monitor-to-action mapping](../ops/PILOT-RUNBOOK.md#monitor-to-action). |
| 1b, 6b (pilot exit) | `ops/PILOT-RUNBOOK.md`, [pilot exit and conversion](../ops/PILOT-RUNBOOK.md#pilot-exit-and-conversion). |

Operator outcomes are recorded to the execution worktree's `.evidence/` root as
`PILOT2-###-operator.txt` files — ids, hostnames, and PASS/FAIL lines only, never
secrets.
