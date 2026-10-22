# 0009: Municipal rules are reviewed firm data, not platform content

- **Status**: Accepted
- **Date**: 2026-10-22
- **Gate**: AXRM-001 of the local-only plan `axorim-regional-mvp` (see `docs/project-status.md`)
- **Applies to**: municipal obligation kinds, rule versions, municipal holidays,
  municipal obligations and the ISS review in the regional MVP

## Question

The regional MVP tracks municipal ISS work for clients in Rio de Janeiro, Niteroi, Sao
Goncalo and the Baixada Fluminense municipalities the firm names at onboarding. Each
municipality has its own deadlines, holidays, filing system and withholding rules, and
they change by decree. Somebody has to say what the rule is. Is that Axorim, shipping a
rule catalog, or the firm?

## Decision

Municipal rules are the firm's professional configuration. Axorim ships the rule
*structure* and never the rule *content*.

- **Responsibility.** One firm member authors a rule version; a different member holding
  `municipal.rules.review` reviews and activates it. Every rule cites an official source
  with title, URL and access date.
- **No seeded content.** No migration, fixture or command ships a municipal deadline,
  holiday or filing rule. Municipal obligation *kinds* are platform `ObligationType`
  rows with no municipality-specific content. The R1 platform tables
  (`ObligationType`, `ObligationDueRule`, `Holiday`) keep the federal rules.
- **Records.** All in `apps/obligations`: a rule version per tenant and municipality
  (number, state, validity window, author, reviewer, review note) with child obligation
  rules and municipal holidays; client-scoped applicability overrides; and municipal
  `Obligation` rows that are never portal-visible.
- **Lifecycle.** `rascunho -> em_revisao -> ativa -> aposentada`, plus
  `em_revisao -> rascunho` when a reviewer returns it with a note. Activation refuses
  when the reviewer is the author, when any rule lacks a source URL or access date, or
  when the version overlaps another active version for the same municipality and dates.
- **Immutability.** An active version never changes. A change is a new version whose
  start date falls after the last closed cycle period that used the old one.
- **Closed periods are never regenerated.** Generation runs when a cycle opens and when a
  version activates, for open periods only, and it's idempotent on tenant, client,
  obligation type, competence and municipality.
- **No guessing.** If a client needs a municipality with no active version, generation
  opens a `regra_municipal_ausente` exception owned by the engagement's responsible
  person. It never invents a deadline.
- **Evidence, not confirmation.** Staff record the guide, receipt or protocol from the
  official municipal system. That record is `staff_recorded` and reviewed, and the copy
  says "Registrado manualmente no sistema da prefeitura - nao e confirmacao da
  prefeitura".
- **No ISS calculation.** The ISS review sums values *stated* in reviewed documents and
  compares them with recorded guides. It never computes ISS due.

A municipality is *supported* for a firm only once that firm's reviewed version for it is
active (gate OG-R7 in `ops/AXORIM-R1-GO-NO-GO.md`).

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| A platform rule catalog maintained by Axorim through pull requests | Reuses more R1 code, but makes Axorim the publisher of municipal law for every firm and needs a deploy for every decree. Reversible later as shared templates a firm can adopt and review. |
| Seed the pilot municipalities' current rules | The values would be unreviewed by the accountable professional and would silently age. |
| Let rule edits apply to closed periods | Rewrites what the firm knew at the time; the evidence trail would stop matching the work that was done. |

## Consequences

- A firm can't see municipal deadlines until two of its members have written and reviewed
  them. That is slower to start and is the point: the deadline is the firm's
  professional claim.
- The rule shape and due-date grammar reuse the existing obligations grammar and holiday
  resolution, extended with municipal holidays.
- Shared templates, if they come later, need their own decision.
