# R1 cohort go/no-go ledger

This ledger lists every operator gate (OG) and owner decision (OD) the R1 release
depends on, with the evidence that would close each one. It follows the convention in
`docs/project-status.md`: a row that needs a real host, mailbox, bucket, account,
person or decision reads `open (needs …)` until its evidence exists. It's never
`blocked`, and it's never closed by anything in this repository.

## Rules

- **No line is GO without its evidence file.** A row moves to GO only when the named
  evidence exists and the owner has read it. An absent file is NO-GO.
- **This file holds structure, not observations.** Statuses change here only in a
  commit that names the evidence it read. Nobody marks a row GO to make a cohort start.
- **Engineering is not a gate closure.** Code, tests, runbooks and drafts prepare a gate.
  They don't close it. A runbook that describes a drill is not a drill.
- **OG-09 is per cohort.** Each cohort below needs its own owner go/no-go line.

## Operator gates

| id | gate | blocks | evidence | status |
| --- | --- | --- | --- | --- |
| OG-01 | Encarregado (DPO) named in `docs/lgpd.md` §3 (decision OD-10) | Real-data cohorts | Commit naming the encarregado in `docs/lgpd.md` | open (needs contact-channel verification, `.evidence/AXRM-OG01-channel.txt`; naming commit = todo 42 of `axorim-regional-mvp`; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |
| OG-02 | Pilot-readiness-v2 F1-F4 completed and pilot start authorized by the owner (policy in OD-02) | Cohort 2 | `docs/project-status.md` pilot authorization line; `.evidence/AXRM-002-pilot2-reconciliation.txt` | open (closes by the owner's OD-02 decision: the v2 obligations carry to the current release, or a re-run); F1-F4 recorded complete and pilot start authorized 2026-09-26 for `a962173` (tag `v0.2.0-rc1`) only, not `71225ef` or the regional MVP (source: AXRM-002 reconciliation; plan `axorim-regional-mvp` A1, A12) |
| OG-03 | Real email from the production host via Resend SMTP reaches an owner mailbox, with SPF, DKIM and DMARC verified | Live email claims | `.evidence/AXR1-010-operator.txt` | GO (production receipt and authentication verified; owner read-back 2026-10-15) |
| OG-04 | OCI bucket prefixes `quarantine/`, `exports/`, `effects-journal/` created with versioning and lifecycle rules (journal retained 5 years) | Production use of the effects journal, quarantine and exports | `.evidence/AXR1-010-operator-bucket.txt` | GO (prefixes, versioning and journal lifecycle verified; owner read-back 2026-10-15) |
| OG-05 | ClamAV sidecar fits host memory and freshclam is updating | Malware scanning on | `.evidence/AXR1-012-operator.txt` | GO (host fit, freshclam and scan probes verified on 1.4.6; owner authorization 2026-10-15) |
| OG-06 | Restore-replay drill on an isolated host | Restore claims | `.evidence/AXR1-027-operator.txt` | GO (isolated restore and supplemental replay witness verified; owner read-back 2026-10-15) |
| OG-07 | Design-partner sessions run per protocol, with the pass bar met | Cohort 3 and general availability | `.evidence/AXR1-029-OG07-sessions.txt` (anonymized session notes) | open (needs recruited partner firms and moderated sessions) |
| OG-08 | Professional legal, LGPD and accounting review of terms, DPA, retention periods and lawful bases, including employee and counterparty personal data and platform support access (owner-approved grants of at most 72 hours, AXR1-023) | Purge activation, paid launch | `.evidence/AXR1-029-OG08-review.txt` (signed review note) | open (needs professional reviewer) |
| OG-09 | Owner go/no-go per cohort | Each cohort | The cohort lines below | open (needs owner decision per cohort) |

## Owner decisions

| id | decision | blocks | evidence | status |
| --- | --- | --- | --- | --- |
| OD-01 | Adopt "Axorim" publicly | Public copy, committing strategy docs | `.evidence/AXR1-029-OD01-name.txt` (INPI search and domain record) | open (needs trademark search and domain clearance) |
| OD-02 | Relationship to open pilot-readiness F1-F4 and pilot start | Cohort 2 | `docs/project-status.md` updated | open (needs owner decision after todo 2 of `axorim-regional-mvp`, reading `.evidence/AXRM-002-pilot2-reconciliation.txt`; closes OG-02; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |
| OD-03 | Integra Contador in R2, not R1 | Nothing in R1 (reversible) | Plan record of the decision | decided by owner 2026-10-22: superseded by the approved regional scope (`docs/product/pivot-supersession.md`, regional section; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |
| OD-04 | Launch billing path: manual invoicing with entitlement bands | Paid launch | `.evidence/AXR1-029-OD04-invoice.txt` (first invoice paid) | open (needs payment provider choice and a first invoice) |
| OD-05 | AI provider and DPA for shadow and assisted modes | Shadow runs on real data | `.evidence/AXR1-029-OD05-dpa.txt` (signed DPA) | open (needs provider choice and signed DPA) |
| OD-06 | Certificate and credential custody for R2 connectors | R2 connectors | Threat model review record | open (custody decided by Q1 2026-10-22; needs the ADR-0008 threat-model section reviewed in F2; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |
| OD-07 | Pricing publication | Public pricing (the pricing page says "Fale conosco" until then) | `.evidence/AXR1-029-OD07-pricing.txt` (partner interviews) | open (needs willingness-to-pay interviews) |
| OD-08 | Supply existing accountant interview notes | Nothing (non-blocking) | Anonymized notes shared by the owner | open (needs owner to share notes) |
| OD-09 | Add `.omo/` to this clone's `.git/info/exclude` | Nothing (non-blocking) | Command run by the owner | open (needs owner to confirm the exclude entry) |
| OD-10 | Encarregado (DPO) named | Real-data cohorts (same person as OG-01) | `docs/lgpd.md` §3 updated | decided by owner 2026-10-22 (identity supplied); evidence = `docs/lgpd.md` §3 updated by todo 42 of `axorim-regional-mvp`; OG-01 channel verification stays open (source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |

## Also tracked by the plan

These rows sit outside the OG-01..OG-09 and OD-01..OD-10 ranges but are part of the same
contract, so they're listed here rather than dropped.

| id | item | evidence | status |
| --- | --- | --- | --- |
| OG-10 | `btree_gist`, `unaccent`, `pg_trgm` created on the live pilot database | `.evidence/AXR1-005-operator.txt` | GO (live pilot extensions verified; owner read-back 2026-10-15) |
| A11Y | Manual screen-reader run of `ops/AXORIM-A11Y-CHECKLIST.md` (NVDA + Firefox, VoiceOver + iOS Safari) on the synthetic-data tenant (AXR1-025) | `.evidence/AXR1-025-operator.txt` | open (needs an operator run on real assistive technology) |
| OD-11 | Pilot host upsize for the ClamAV sidecar | Lightsail bundle price recorded with OG-05 | superseded by OD-R2 (measured capacity first; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |
| OD-12 | Split the cohort gate (cohort 2 before the full launch set) | Partner feedback | open (needs owner decision) |
| Contact | A public contact channel for the pricing page | Owner-supplied address or form | open (needs owner to choose the channel) |

## Residual risks from the isolation sweep (AXR1-026)

`docs/residual-risks.md` section 9 records what the wave-4 isolation sweep proved and,
more to the point, what it didn't. The sweep labels each item "accepted for R1". That
label is engineering's recommendation, not the owner's decision, so every row below
stays open until the owner accepts it in writing. Sections 1 to 8 of the same file
cover the earlier residual-hardening plan. They still apply, and this ledger doesn't
restate them.

| id | risk | what it means for a cohort | evidence | status |
| --- | --- | --- | --- | --- |
| RR-01 | SMTP acceptance isn't delivery (section 9.1) | The delivery ledger records that a mail server accepted a message. Nothing watches the mailbox after that: no bounce handling, no DMARC reports, no open tracking. A notice marked as sent may never reach the client, so nobody should read "sent" as "received". OG-03 proves one send reaches one mailbox, not every recipient. | `.evidence/AXR1-029-RR-owner.txt` (owner acceptance) | accepted by owner 2026-10-22 for the bounded one-firm pilot (evidence `.evidence/AXR1-029-RR-owner.txt`; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |
| RR-02 | File content is only checked where an identifier is extractable, and even then only against the client's CNPJ (section 9.2) | The vault checks file type, size and confinement. On readable PDF/XML the uploaded CNPJ is compared and a mismatch raises divergence; images and scans are 'não verificável' — R1 has no OCR — and a document filed under the wrong client still passes as valid, with only the firm's own review catching it. Isolation holds; content truth doesn't. | `.evidence/AXR1-029-RR-owner.txt` (owner acceptance) | accepted by owner 2026-10-22 for the bounded one-firm pilot (evidence `.evidence/AXR1-029-RR-owner.txt`; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |
| RR-03 | Isolation is proven by denial on declared surfaces (section 9.3) | Each probe asks for another firm's row and sees a refusal. A surface nobody declared, such as a new endpoint, an error message or a log line, isn't probed until someone adds it to the enumeration lists. | `.evidence/AXR1-029-RR-owner.txt` (owner acceptance) | accepted by owner 2026-10-22 for the bounded one-firm pilot (evidence `.evidence/AXR1-029-RR-owner.txt`; source: plan `axorim-regional-mvp` A12, recorded by AXRM-001) |

## Cohorts

| cohort | requires | evidence | status |
| --- | --- | --- | --- |
| Partner sessions on the synthetic-data tenant (before cohort 2, per OD-12) | Engineering gates of the releases shown; no real data | `.evidence/AXR1-029-OG07-sessions.txt` | open (needs partner sessions to be scheduled) |
| Cohort 2: one design-partner pilot firm, real data, no billing | OG-01, OG-02, OG-03, OG-04, OG-05 (or scanning off with staff-only files), OG-08 for the partner DPA, OD-02, OD-10, RR-01 to RR-03, OG-09 | Each row's evidence above plus `.evidence/AXR1-029-OG09-cohort2.txt` | open (needs every listed gate and the owner go/no-go) |
| Cohort 3 and general availability | Cohort 2 requirements plus OG-06, OG-07, OD-01 for public naming | Each row's evidence above plus `.evidence/AXR1-029-OG09-cohort3.txt` | open (needs every listed gate and the owner go/no-go) |
| Paid cohorts | Cohort 3 requirements plus OG-08, OD-04, OD-07 | Each row's evidence above plus `.evidence/AXR1-029-OG09-paid.txt` | open (needs every listed gate and the owner go/no-go) |

## Regional MVP

The owner approved the regional MVP (local-only plan `axorim-regional-mvp`, ids
`AXRM-###`) on 2026-10-22. Its gates are listed here, recorded by AXRM-001 from the plan's
gate ledger (A12). The rules at the top of this file apply unchanged, plus one more: a
lower layer never closes a higher one. A simulated pass doesn't close a homologation
gate, a homologation pass doesn't close a live gate, and a live pass doesn't authorize a
cohort.

### Engineering acceptance

| id | gate | closed by | blocks | evidence | status |
| --- | --- | --- | --- | --- | --- |
| EG-1 | Standard gates green at every PR head | each todo | merge of that PR | `.evidence/AXRM-NNN-gates.txt` | open (needs each PR's gate run) |
| EG-2 | Isolation inventory covers every new model, view and task, with vacuity controls | todo 36 | wave 6 merge | `.evidence/AXRM-036-*.txt` | open (needs todo 36) |
| EG-3 | Golden regional journeys and failure paths pass at 1440x900 and 390x844 with axe | todo 37 | wave 6 merge | `.evidence/AXRM-037-*.txt` | open (needs todo 37) |
| EG-4 | Provider restore-replay tests pass with no repeated paid call or registration | todos 9, 38 | provider unpause on any host | `.evidence/AXRM-009-*.txt`, `.evidence/AXRM-038-*.txt` | open (needs todos 9 and 38) |
| EG-5 | Named reads within 25 queries and p95 targets with the regional sections | todos 4, 39 | cohort | `.evidence/AXRM-004-*.txt`, `.evidence/AXRM-039-*.txt` | open (needs todos 4 and 39) |

### Provider qualification

Run by the operator through `manage.py qualify_provider` with no real taxpayer data:
SERPRO scenarios after the wave-2 merge, Focus scenarios after the wave-3 merge. The
questions to the providers are kept with the plan; a missing answer is never read as
"no".

| id | gate | environment | blocks | evidence | status |
| --- | --- | --- | --- | --- | --- |
| PQ-SERPRO-1 | Fiscal-situation protocol and emission parsed, `tempoEspera` honoured | SERPRO trial gateway (mock data) | LQ-3 | `.evidence/AXRM-PQ-SERPRO-1.txt` | open (needs a trial run after the wave-2 merge) |
| PQ-SERPRO-2 | Payment consultation paging and normalization | trial | LQ-4 | `.evidence/AXRM-PQ-SERPRO-2.txt` | open (needs a trial run after the wave-2 merge) |
| PQ-SERPRO-3 | MEI DAS guide PDF stored and scanned | trial | LQ-5 | `.evidence/AXRM-PQ-SERPRO-3.txt` | open (needs a trial run after the wave-2 merge) |
| PQ-SERPRO-4 | Termo XML accepted; token and 304 semantics | trial | OG-R5 | `.evidence/AXRM-PQ-SERPRO-4.txt` | open (needs a trial run after the wave-2 merge) |
| PQ-SERPRO-5 | Billing semantics re-read from the official pages; contract terms let Axorim act as contratante for a partner firm's clients under the signed termo; data-processing clauses captured for the OG-08 review | official pages and contract | OD-R1, OG-R3, OG-08 | `.evidence/AXRM-PQ-SERPRO-5.txt` | open (needs the contract read and SERPRO's written answers) |
| PQ-FOCUS-1 | Received NF-e feed with `versao`, summary versus full XML, and whether enabling manifestation registers Ciencia automatically | Focus homologacao and a written Focus answer | LQ-1; todo 19 refuses the received NF-e stream without it | `.evidence/AXRM-PQ-FOCUS-1.txt` | open (needs homologacao responses and Focus's written answer) |
| PQ-FOCUS-2 | Received NFS-e Nacional feed, cancellation and substitution fields | homologacao | LQ-2 | `.evidence/AXRM-PQ-FOCUS-2.txt` | open (needs homologacao responses) |
| PQ-FOCUS-3 | Written answer: does any route return NFS-e where the company is prestador or intermediario | Focus support | issued-NFS-e capture mode; OD-R5 | `.evidence/AXRM-PQ-FOCUS-3.txt` | open (needs Focus's written answer) |
| PQ-FOCUS-4 | Written answer on billing of summaries, events, Ciencia, CC-e XML and PDFs, plan pooling across CNPJs, test-period scope, export at exit and third-party-client terms | Focus commercial | OD-R1 | `.evidence/AXRM-PQ-FOCUS-4.txt` | open (needs Focus's written answer and terms URL) |
| PQ-FOCUS-5 | Certificate upload path compatible with Q1 (firm or client registers the certificate in Focus's own interface) | Focus support | OG-R4 | `.evidence/AXRM-PQ-FOCUS-5.txt` | open (needs Focus's written answer) |

### Operator evidence

OG-01 and OG-08 above also block the regional cohort and real-data qualification. The
A11Y row above applies to the regional journeys too, with evidence
`.evidence/AXRM-A11Y-operator.txt`.

| id | gate | blocks | evidence | status |
| --- | --- | --- | --- | --- |
| OG-R1 | Integrated scanning live on the pilot host (`CLAMAV_ENABLED=true`, compose profile, readiness check green, EICAR through the application path, capacity under synthetic load) | any real upload or capture | `.evidence/AXRM-OGR1-scanning.txt` | open (needs the wave-0 merge and an operator run on the pilot host) |
| OG-R2 | Provider secrets installed (mode 0600, names only in read-back) | provider unpause | `.evidence/AXRM-OGR2-secrets.txt` | open (needs operator install on the host) |
| OG-R3 | SERPRO contract active with the PQ-SERPRO-5 clauses captured; consumer key and secret issued; contratante e-CNPJ converted to PEM secrets and mounted only into `serpro-broker` (fingerprints only) | LQ-3 to LQ-5 | `.evidence/AXRM-OGR3-serpro.txt` | open (needs OD-R6, OD-R1 and an active contract) |
| OG-R4 | Focus plan active; each pilot company registered with the agreed flags and start month; certificates registered in Focus's own interface; Axorim status sync shows CNPJ and validity | LQ-1, LQ-2 | `.evidence/AXRM-OGR4-focus.txt` | open (needs OD-R1, PQ-FOCUS-5 and an active plan) |
| OG-R5 | Firm-signed termo de autorizacao accepted by SERPRO | LQ-3 to LQ-5 | `.evidence/AXRM-OGR5-termo.txt` | open (needs the firm's e-CNPJ signature and OG-R3) |
| OG-R6 | Per-client authority recorded: procuracoes 00002 and 00004 visible in e-CAC, client capture authorization, client authorization for Ciencia in approved batches, certificate validity; the accountable professional's written note on mandatory-manifestation cases and how conclusive manifestations are done in the official systems | per-client activation | `.evidence/AXRM-OGR6-authority.txt` (counts and ids only) | open (needs client procuracoes, authorizations and the professional's note) |
| OG-R7 | Municipal rule versions for each pilot municipality authored and reviewed by two firm members, with official sources | municipal obligations for that municipality | `.evidence/AXRM-OGR7-rules.txt` | open (needs firm-authored and reviewed rule versions) |
| OG-R8 | Restore drill on an isolated host, including provider journal reconciliation | cohort | `.evidence/AXRM-OGR8-restore.txt` | open (needs an operator drill) |
| OG-R9 | Monitoring and backups: UptimeRobot M3 keyword updated to the current release, provider-failure alerting tested, `/healthz` reporting `backup: fresh` and the latest verify-live green on the cohort date | cohort | `.evidence/AXRM-OGR9-monitoring.txt` | open (needs operator updates on the cohort date) |
| OG-R10 | Pilot inventory: CNPJ list, municipality per client, regime evidence, representative month volumes by client, direction and issuer municipality | OD-R1, cohort sizing | `.evidence/AXRM-OGR10-inventory.txt` (aggregates only) | open (needs the firm's inventory) |
| OG-R11 | Named pilot participants: at least two firm members with distinct initiator and approver roles, the second member who reviews municipal rules, Axorim's onboarding and support owner, and the escalation path | real-data qualification and cohort | `.evidence/AXRM-OGR11-participants.txt` (roles only) | open (needs the firm to name participants) |
| OG-R12 | Partner validation session on the synthetic regional tenant: the firm completes the regional journey and correctly explains coverage, manual-versus-official labels and an uncertain call | OD-R3 | `.evidence/AXRM-OGR12-session.txt` (anonymized) | open (needs the todo 35 seed and a moderated session) |
| RG-01 | R1 competitor research row | partner-session programmes (OG-07), public copy and pricing; not a prerequisite of this free one-firm cohort | R1 plan rule | open (needs the R1 research; recorded so it isn't mistaken as closed) |

### Authorized live qualification

On a consenting pilot client, after OD-R3, with receipts stored and cost measured.

| id | gate | blocks | evidence | status |
| --- | --- | --- | --- | --- |
| LQ-1 | NF-e capture and one approved Ciencia batch | OD-R4 | `.evidence/AXRM-LQ-1.txt` | open (needs OD-R3, PQ-FOCUS-1 and OG-R4) |
| LQ-2 | NFS-e capture | OD-R4 | `.evidence/AXRM-LQ-2.txt` | open (needs OD-R3, PQ-FOCUS-2 and OG-R4) |
| LQ-3 | Fiscal-situation report | OD-R4 | `.evidence/AXRM-LQ-3.txt` | open (needs OD-R3, PQ-SERPRO-1, OG-R3 and OG-R5) |
| LQ-4 | Payment consultation | OD-R4 | `.evidence/AXRM-LQ-4.txt` | open (needs OD-R3, PQ-SERPRO-2, OG-R3 and OG-R5) |
| LQ-5 | MEI DAS guide | OD-R4 | `.evidence/AXRM-LQ-5.txt` | open (needs OD-R3, PQ-SERPRO-3, OG-R3 and OG-R5) |
| LQ-6 | Deliberate denied call (client without procuracao, billed 403 accepted within the cap) and quota-cap refusal | OD-R4 | `.evidence/AXRM-LQ-6.txt` | open (needs OD-R3 and OG-R3) |

### Owner decisions

| id | decision | blocks | evidence | status |
| --- | --- | --- | --- | --- |
| Q1 | Provider contracting and certificate custody | OG-R3, OG-R4 | ADR 0008 | decided 2026-10-22: Axorim contracts SERPRO (as contratante) and Focus NFe; client certificates are registered only in Focus's own interface |
| Q2 | NF-e Ciencia da Operacao | Ciencia batches | ADR 0008 | decided 2026-10-22: only exact-approved finite batches, approved by a second firm member |
| Q3 | Merge and deploy cadence | each wave merge | `docs/project-status.md` "Regional MVP" | decided 2026-10-22: merge per wave, with the owner's authorization per wave |
| D-PGMEI | Include the staff-triggered PGMEI DAS guide | todo 17 | plan approval record | decided 2026-10-22: included (kept at plan approval) |
| OD-R1 | Provider contracts, Focus plan tier and monthly spending ceilings | OG-R3, OG-R4 | owner decision record | open (needs OG-R10 volumes and the PQ-SERPRO-5 and PQ-FOCUS-4 answers) |
| OD-R2 | Host upsize if OG-R1 capacity requires it (supersedes OD-11) | OG-R1 | measured shortfall and recorded cost | open (needs a measured capacity shortfall, if any) |
| OD-R3 | Real-data qualification go/no-go (LQ-1 to LQ-6) | LQ-1 to LQ-6 | owner decision record | open (needs OG-01, OG-08, OG-R1 to OG-R7, OG-R11, OG-R12 and OD-06) |
| OD-R4 | Regional cohort activation (see "Regional cohort" below) | cohort | `.evidence/AXRM-OG09-regional-cohort.txt` | open (needs every prerequisite listed below and the owner's decision) |
| OD-R5 | Provider change if a required scenario fails | issued-NFS-e capture mode | comparison with evidence | open (needs a failed scenario first; import path covers the gap meanwhile) |
| OD-R6 | Axorim legal entity and e-CNPJ for the SERPRO contract | OG-R3 | owner decision record | open (needs the owner's CNPJ and ICP-Brasil e-CNPJ A1) |

R1 rows touched by this plan, with their source in each status cell above: OG-01, OG-02,
OD-02, OD-03, OD-06, OD-10, OD-11 and RR-01 to RR-03.

### Regional cohort

For this firm, this line replaces R1's "Cohort 2" line. R1's alternative of running with
scanning off and staff-only files does not apply: scanning is required.

| cohort | requires | evidence | status |
| --- | --- | --- | --- |
| Regional cohort: one partner firm, real data, no billing, flags `scan_required`, `federal_checks`, `document_capture`, `municipal_work` on for its tenant, `DELIVERY_PAUSED=False`, providers unpaused | OG-01, OG-03, OG-04, OG-08, OD-02 (closing OG-02), OD-10, RR-01 to RR-03 as accepted, EG-1 to EG-5, LQ-1 to LQ-6, OG-R1 to OG-R12, OD-R1, OD-R3, A11Y, and OD-R4 (OG-09 for this cohort) | Each row's evidence above plus `.evidence/AXRM-OG09-regional-cohort.txt` | open (needs every listed gate and the owner's decision) |
