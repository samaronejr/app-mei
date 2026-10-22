# Product direction change: supersession record

This record says which earlier product restrictions no longer bind the work, which
constraints still do, and what the R1 release is on the engineering side. Later pull
requests cite it instead of re-arguing the change. The status index,
`docs/project-status.md`, points here from its "Axorim pivot" section.

## What changed

The product stops being defined as an MEI-only tool for accounting firms. The governing
direction is now the Axorim R1 release, executed under the local-only plan
`axorim-pivot-r1` with evidence ids of the form `AXR1-###`. "Axorim" is an internal
working name: the repository stays `app-mei`, the README headline doesn't change, and
customer-facing copy keeps "app-mei" until the name is cleared.

The MEI calendar, the fiscal threshold logic, the portal and the document vault aren't
thrown away. They become the first workflow pack and the foundations the new work
builds on.

## R1 on the engineering side

- **Unit of work.** One client and one period form a service cycle. It links requests,
  documents, tasks, exceptions, approvals, messages and outcomes, and it ends either
  complete (every required item accepted or waived with a reason, every exception
  resolved, every client-facing effect approved and executed or explicitly cancelled,
  an evidence index generated) or closed with pending items, with the reasons written
  down.
- **Workflow packs.** A monthly MEI pack driven by the existing deterministic calendar,
  and a regime-neutral monthly document collection and review pack that doesn't compute
  any tax regime.
- **Channels.** The firm web app, the client web portal (responsive, firm-branded) and
  outbound email through the existing SMTP relay. Nothing else.
- **Outcomes the product records, and what backs each one.**
  - Document received: portal upload, hashed and attributed.
  - Document reviewed: reviewer, time, version hash and corrections.
  - Message accepted by the SMTP relay: the provider's reply is stored, and it's never
    called "delivered".
  - Portal publication viewed or downloaded by an authenticated client user.
  - Client acknowledgement, as an explicit portal action.
  - Payment or filing evidence, recorded as supplied by staff or by the client pending
    review, never as confirmed by the authority.
- **Out of R1.** Tax filing, payment initiation, bookkeeping, payroll, government
  connectors, WhatsApp, a store app, certificate custody (ADR 0006), and any AI output
  shown to users or used for an effect. AI work in R1 is limited to an offline
  evaluation harness and a shadow runner on synthetic data.
- **Rollout.** Every new surface ships behind a per-tenant feature flag that is off by
  default, so a merge to `main` (which deploys to the pilot host) changes nothing for a
  firm until its flag is turned on.
- **Architecture.** ADRs 0002 to 0007 in `docs/decisions/` record the decisions:
  frontend stack, bounded contexts, state and outbox, search, certificates, and the
  portal without an offline cache. The status vocabulary is in
  `docs/product/vocabulary.md`.

## Legacy restrictions, classified

Sources: `solo-build-plan-accounting-mei-saas.md`, `deep-research-report.md` and the
frozen `.omo/plans/accounting-mei-saas.md`.

| Earlier restriction or assumption | Class | Treatment now |
| --- | --- | --- |
| MEI-only positioning, with payroll, Open Finance, NF-e, white-label, advanced analytics and WhatsApp excluded | Obsolete product restriction | **Superseded** by this record. Later scope is sequenced release by release; nothing beyond R1 is promised here. |
| Hetzner-class VPS, R2 or Backblaze storage, `django-otp` | Obsolete infrastructure suggestion | Already superseded in practice: Lightsail hosting, OCI object storage, and allauth MFA (ADR 0001). |
| Tenant and client isolation, row-level security, the portal database role, append-only audit, the self-only CSP, mandatory MFA, credential-free logs, alphanumeric CNPJ support | Still-valid safety constraint | **Preserved.** Every R1 table and view must pass the existing isolation meta-tests, and none of these controls may be weakened. |
| Sequential pull requests on `main`, a serial shared test database, committed built assets, and the docs path-reference guard (`tests/scope/test_comment_references.py`) | Still-valid engineering constraint | **Binding** on every R1 pull request. |
| Pilot-readiness-v2 final verification (F1-F4) and the owner's pilot authorization, the unnamed encarregado (`docs/lgpd.md` section 3), and terminating the old OCI host after the `v0.2.0-rc1` tag | Still-open operational obligation | **Kept separate.** The pivot neither closes nor waives any of them. R1 reaches the pilot firm only behind its flag, and only after the pilot is authorized. |
| Certificates stored as metadata only (`apps/clients/models/company.py:137-150`) | Still-valid security boundary | **Kept** for R1 (ADR 0006). Changing it needs a separate owner decision. |

## What this record does not do

- It doesn't edit or reopen any earlier plan. Closed plans stay immutable history, as
  `docs/project-status.md` describes.
- It doesn't claim any R1 feature exists. Each one lands in its own pull request with
  its own tests and evidence.
- It doesn't close any operator gate. Email proof from the production host, bucket
  configuration, legal review, a named encarregado and pilot authorization each close
  only with their own evidence.

## Regional MVP supersession (2026-10-22)

The owner approved a regional MVP on 2026-10-22, executed under the local-only plan
`axorim-regional-mvp` with ids of the form `AXRM-###`. It changes two R1 restrictions
and nothing else:

| R1 restriction | Treatment now |
| --- | --- |
| No government connectors in R1; Integra Contador in R2 at the earliest (R1 decision OD-03) | **Superseded for the regional MVP.** Federal checks through SERPRO Integra Contador and fiscal document capture through Focus NFe are in scope, limited to the operations ADR 0008 allows. Invoice issuance, declarations, payments and conclusive manifestations stay out. |
| Certificate custody blocked pending a decision (ADR 0006) | **Decided** by the owner's answer Q1 and recorded in ADR 0008: client certificates stay at Focus, Axorim stores status only, and its own SERPRO certificate lives only in the broker container. |

The R1 safety controls remain binding: tenant and client isolation, row-level security,
the portal role, append-only audit, the self-only CSP, mandatory MFA, credential-free
logs, per-tenant flags off by default, exact-effect approvals and the status vocabulary.
Provider features add a global `PROVIDERS_PAUSED` switch that defaults on. Municipal
rules are firm-reviewed data (ADR 0009). None of this closes an operator gate; the
regional gates are in `ops/AXORIM-R1-GO-NO-GO.md`.
