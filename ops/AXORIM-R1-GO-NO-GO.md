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
| OG-01 | Encarregado (DPO) named in `docs/lgpd.md` §3 (decision OD-10) | Real-data cohorts | Commit naming the encarregado in `docs/lgpd.md` | open (needs owner to name the encarregado) |
| OG-02 | Pilot-readiness-v2 F1-F4 completed and pilot start authorized by the owner (policy in OD-02) | Cohort 2 | `docs/project-status.md` pilot authorization line | open (needs F1-F4 verdicts and owner authorization) |
| OG-03 | Real email from the production host via Resend SMTP reaches an owner mailbox, with SPF, DKIM and DMARC verified | Live email claims | `.evidence/AXR1-010-operator.txt` | open (needs production host send and mailbox receipt) |
| OG-04 | OCI bucket prefixes `quarantine/`, `exports/`, `effects-journal/` created with versioning and lifecycle rules (journal retained 5 years) | Production use of the effects journal, quarantine and exports | `.evidence/AXR1-010-operator-bucket.txt` | open (needs bucket configuration by the operator) |
| OG-05 | ClamAV sidecar fits host memory and freshclam is updating | Malware scanning on | `.evidence/AXR1-012-operator.txt` | open (needs host sizing, OD-11, and a live freshclam check) |
| OG-06 | Restore-replay drill on an isolated host | Restore claims | `.evidence/AXR1-027-operator.txt` | open (needs isolated host and drill window) |
| OG-07 | Design-partner sessions run per protocol, with the pass bar met | Cohort 3 and general availability | `.evidence/AXR1-029-OG07-sessions.txt` (anonymized session notes) | open (needs recruited partner firms and moderated sessions) |
| OG-08 | Professional legal, LGPD and accounting review of terms, DPA, retention periods and lawful bases, including employee and counterparty personal data and platform support access (owner-approved grants of at most 72 hours, AXR1-023) | Purge activation, paid launch | `.evidence/AXR1-029-OG08-review.txt` (signed review note) | open (needs professional reviewer) |
| OG-09 | Owner go/no-go per cohort | Each cohort | The cohort lines below | open (needs owner decision per cohort) |

## Owner decisions

| id | decision | blocks | evidence | status |
| --- | --- | --- | --- | --- |
| OD-01 | Adopt "Axorim" publicly | Public copy, committing strategy docs | `.evidence/AXR1-029-OD01-name.txt` (INPI search and domain record) | open (needs trademark search and domain clearance) |
| OD-02 | Relationship to open pilot-readiness F1-F4 and pilot start | Cohort 2 | `docs/project-status.md` updated | open (needs owner decision) |
| OD-03 | Integra Contador in R2, not R1 | Nothing in R1 (reversible) | Plan record of the decision | open (needs owner confirmation) |
| OD-04 | Launch billing path: manual invoicing with entitlement bands | Paid launch | `.evidence/AXR1-029-OD04-invoice.txt` (first invoice paid) | open (needs payment provider choice and a first invoice) |
| OD-05 | AI provider and DPA for shadow and assisted modes | Shadow runs on real data | `.evidence/AXR1-029-OD05-dpa.txt` (signed DPA) | open (needs provider choice and signed DPA) |
| OD-06 | Certificate and credential custody for R2 connectors | R2 connectors | Threat model review record | open (needs threat model review) |
| OD-07 | Pricing publication | Public pricing (the pricing page says "Fale conosco" until then) | `.evidence/AXR1-029-OD07-pricing.txt` (partner interviews) | open (needs willingness-to-pay interviews) |
| OD-08 | Supply existing accountant interview notes | Nothing (non-blocking) | Anonymized notes shared by the owner | open (needs owner to share notes) |
| OD-09 | Add `.omo/` to this clone's `.git/info/exclude` | Nothing (non-blocking) | Command run by the owner | open (needs owner to confirm the exclude entry) |
| OD-10 | Encarregado (DPO) named | Real-data cohorts (same person as OG-01) | `docs/lgpd.md` §3 updated | open (needs owner decision) |

## Also tracked by the plan

These rows sit outside the OG-01..OG-09 and OD-01..OD-10 ranges but are part of the same
contract, so they're listed here rather than dropped.

| id | item | evidence | status |
| --- | --- | --- | --- |
| OG-10 | `btree_gist`, `unaccent`, `pg_trgm` created on the live pilot database | `.evidence/AXR1-005-operator.txt` | open (needs operator read-back of the evidence) |
| A11Y | Manual screen-reader run of `ops/AXORIM-A11Y-CHECKLIST.md` (NVDA + Firefox, VoiceOver + iOS Safari) on the synthetic-data tenant (AXR1-025) | `.evidence/AXR1-025-operator.txt` | open (needs an operator run on real assistive technology) |
| OD-11 | Pilot host upsize for the ClamAV sidecar | Lightsail bundle price recorded with OG-05 | open (needs owner approval and cost record) |
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
| RR-01 | SMTP acceptance isn't delivery (section 9.1) | The delivery ledger records that a mail server accepted a message. Nothing watches the mailbox after that: no bounce handling, no DMARC reports, no open tracking. A notice marked as sent may never reach the client, so nobody should read "sent" as "received". OG-03 proves one send reaches one mailbox, not every recipient. | `.evidence/AXR1-029-RR-owner.txt` (owner acceptance) | open (needs owner acceptance before cohort 2) |
| RR-02 | File content isn't checked against the client's CNPJ (section 9.2) | The vault checks file type, size and confinement. A document filed under the wrong client still passes as valid, and only the firm's own review catches it. Isolation holds; content truth doesn't. | `.evidence/AXR1-029-RR-owner.txt` (owner acceptance) | open (needs owner acceptance before cohort 2) |
| RR-03 | Isolation is proven by denial on declared surfaces (section 9.3) | Each probe asks for another firm's row and sees a refusal. A surface nobody declared, such as a new endpoint, an error message or a log line, isn't probed until someone adds it to the enumeration lists. | `.evidence/AXR1-029-RR-owner.txt` (owner acceptance) | open (needs owner acceptance before cohort 2) |

## Cohorts

| cohort | requires | evidence | status |
| --- | --- | --- | --- |
| Partner sessions on the synthetic-data tenant (before cohort 2, per OD-12) | Engineering gates of the releases shown; no real data | `.evidence/AXR1-029-OG07-sessions.txt` | open (needs partner sessions to be scheduled) |
| Cohort 2: one design-partner pilot firm, real data, no billing | OG-01, OG-02, OG-03, OG-04, OG-05 (or scanning off with staff-only files), OG-08 for the partner DPA, OD-02, OD-10, RR-01 to RR-03, OG-09 | Each row's evidence above plus `.evidence/AXR1-029-OG09-cohort2.txt` | open (needs every listed gate and the owner go/no-go) |
| Cohort 3 and general availability | Cohort 2 requirements plus OG-06, OG-07, OD-01 for public naming | Each row's evidence above plus `.evidence/AXR1-029-OG09-cohort3.txt` | open (needs every listed gate and the owner go/no-go) |
| Paid cohorts | Cohort 3 requirements plus OG-08, OD-04, OD-07 | Each row's evidence above plus `.evidence/AXR1-029-OG09-paid.txt` | open (needs every listed gate and the owner go/no-go) |
