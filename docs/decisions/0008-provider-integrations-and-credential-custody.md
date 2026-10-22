# 0008: Provider integrations and credential custody for the regional MVP

- **Status**: Accepted
- **Date**: 2026-10-22
- **Gate**: AXRM-001 of the local-only plan `axorim-regional-mvp` (see `docs/project-status.md`)
- **Applies to**: every table, adapter, command and setting that talks to an external
  fiscal data provider (SERPRO Integra Contador, Focus NFe) in the regional MVP
- **Amends**: ADR 0003 (context map) and ADR 0006 (certificate boundary)

## Question

The owner approved a regional MVP on 2026-10-22: federal checks through SERPRO Integra
Contador and fiscal document capture through Focus NFe, for one accounting firm and its
clients in a bounded set of Rio de Janeiro municipalities. R1 shipped no government
connector, and ADR 0006 blocked every connector on a custody decision. Three things need
settling before the first provider table exists: where the integration code and its
state live, which provider operations Axorim may call at all, and who holds which key.

## Decision

### Boundary (Option B)

A new Django app, `apps/integrations`, owns everything that is about talking to a
provider:

- provider connections (environment, pause state, secret *names*, never values);
- client provider links (which client is known to which provider, under which identity);
- authority records (procuracoes, the signed termo, client capture and Ciencia
  authorizations, certificate status read back from the provider);
- sync cursors and runs, the provider call journal, budgets and metering;
- the adapters, the operation allowlist and the simulated providers used in tests.

What a call *produces* lives where R1 already keeps that kind of fact. Fiscal documents,
their revisions and federal observations (payments found, fiscal-situation reports, DAS
guides) live in `apps/obligations`. Reviews, exceptions and approvals live in
`apps/cycles`. The dependency points one way: `integrations` writes into those apps
through their services, and `obligations` and `cycles` never import `integrations`. A
fiscal record therefore stays readable and reviewable after a provider is switched off
or replaced.

### Operation classes and the allowlist

Every provider operation is classified before it can be implemented:

| Class | Meaning | Rule |
| --- | --- | --- |
| read | no state changes anywhere | allowed under the call protocol below |
| issuance | the provider or government produces an official artefact for us (a report, a receipt, a DAS guide); no taxpayer obligation changes | allowed under the call protocol; billed calls are reserved against a budget first |
| registration | an event is registered against the taxpayer at a government system | allowed only where named below, behind an explicit approval; never retried after an ambiguous outcome |
| configuration | state held at the provider (a session, company receipt flags) | operator commands only, confirmed by read-back; never from a web request |
| excluded | changes declarations, options, payments or renunciations, or issues invoices | not implementable |

The adapters carry an allowlist of operation ids. Anything not on it is refused before
any network I/O, and each excluded id gets its own refusal test in the todo that builds
the adapter. The allowed set for this release:

- **SERPRO:** the OAuth2 and mTLS session; the termo de autorizacao
  (`ENVIOXMLASSINADO81`, a registration, operator action under
  `fiscal.authority.manage`); the fiscal-situation report (`SOLICITARPROTOCOLO91`,
  `RELATORIOSITFIS92`); payment consultation and receipt (`PAGAMENTOS71`,
  `COMPARRECADACAO72`); the MEI DAS guide (`GERARDASPDF21`, decision D-PGMEI).
- **Focus NFe:** company configuration (operator commands only); received NF-e and
  NFS-e feeds and their XML, CC-e XML and PDFs; NF-e "Ciencia da Operacao", a
  registration, only for a batch whose exact keys a second firm member approved
  (owner answer Q2).
- **Excluded:** every PGMEI `ATUBENEFICIO23`, every `/Declarar` and declaration
  transmission, parcelamento guides, regime options and renunciations; Focus invoice
  issuance or cancellation of any kind; the conclusive manifestations (`confirmacao`,
  `desconhecimento`, `nao_realizada`), which staff do in the official systems.

Every implemented operation follows one protocol: reserve budget, claim the call
(committed), journal `intent_started`, do the I/O outside any database transaction,
journal the result, finalize behind a fence, then meter. A registration that ends
ambiguously (timeout, connection reset, unparseable reply) is never sent again; it is
reconciled by reading the provider's state.

A payment consultation that finds nothing is stored as a consultation receipt (who
asked, when, which period, which response). It is never stored or shown as a payment
observation, and "no payment found" never becomes "not paid".

### Custody (owner answer Q1, 2026-10-22)

Axorim contracts both providers. Custody follows from that:

- **Client certificates stay at Focus.** The firm or the client registers each client's
  A1 certificate directly in Focus's own interface. Axorim never receives the file or
  its password and has no upload path for either. Axorim stores status only: certificate
  CNPJ, `valid_from`, `valid_to`, provider identity, credential generation and last
  synced time.
- **No client key material anywhere.** No private key, certificate file, certificate
  password, gov.br or municipal password, bearer or procurador token, or presigned URL is
  persisted in the database, object storage, Redis or any other cache, the call journal,
  evidence files or logs.
- **The one key-material exception.** Axorim's own SERPRO contratante e-CNPJ is a single
  platform credential. It is mounted as PEM secrets (certificate, encrypted key,
  passphrase) only into a dedicated `serpro-broker` container, never into `web` or
  `worker`, and never into the database. The broker holds SERPRO tokens in process
  memory only; workers receive a token per call and never cache it. Restarting the
  broker drops every token, and that is the intended way to cut SERPRO access.
- **Provider credentials are deployment secrets** referenced by name from the connection
  row. There are no value columns. Logs and error reports scrub provider tokens,
  `jwt_token`, `Authorization` headers, `autenticar_procurador_token`, presigned URLs
  and base64 payloads.

The existing guard `tests/clients/test_certificate_metadata_only.py` keeps proving that
no client-scoped model can hold key material; the todos that add provider models extend
it to each new model and to a settings scan.

## Threat model

This section is the threat-model record R1 decision OD-06 asked for. It closes OD-06
only after it is reviewed in the final verification wave (F2) and the owner reads it
back; until then OD-06 stays open in `ops/AXORIM-R1-GO-NO-GO.md`.

| Threat | Where it enters | Control |
| --- | --- | --- |
| Malicious provider payload: oversized or zip-bomb XML, XML external entities | captured XML and PDFs, provider JSON | byte cap before any parse; `defusedxml` only; every stored file goes through malware scanning, which is required for these features; parsing failures open an exception, never a partial record |
| Wrong attribution from payload content | a document whose CNPJ belongs to someone else | tenant and client come from our connection and link rows, never from the payload; a CNPJ that doesn't match the client in the expected role is not linked and opens `documento_fiscal_divergente` |
| SSRF through provider-supplied URLs | presigned download links (Focus PDFs) | fetched only from a per-provider host allowlist, at most one redirect, never on behalf of a portal user; the URL is never stored or shown |
| Replay or duplicate of a paid or registering call | retries, worker crashes, database restore | the call protocol above: budget reservation, committed claim, independent journal, fenced finalize; registrations never retried after ambiguity; after a restore, providers stay paused until the journal reconciliation runs |
| Cross-tenant cursor confusion | sync cursors and pages | streams are unique per tenant, client, link, collection, filter digest and epoch, with composite tenant and client foreign keys and row-level security |
| Custody compromise | an application bug or a leaked database dump | no client key material exists in Axorim to leak; the contratante certificate is reachable only inside the broker container; a dump holds secret names and certificate dates, nothing usable |
| Token leakage | logs, error reports, Redis, journal rows | tokens live in broker memory and in the single call that uses them; redaction canaries prove the scrubbing; nothing token-shaped is written to Redis or the journal |
| Staff over-reach | a firm member triggering billed or registering calls | new capabilities with FULL/NONE grants; Ciencia needs a second person's exact-key approval; the termo needs `fiscal.authority.manage`; support grants keep R1 behaviour and never see provider secrets |

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| Put provider state inside `obligations` (Option A) | Couples the deterministic calendar to cursors, budgets and journals, and makes every fiscal record depend on the provider app's migrations. Swapping a provider would touch tax records. |
| Hold client A1 certificates in Axorim, encrypted | Makes Axorim a signing-key holder for every client of every firm, the outcome ADR 0006 rejected. Q1 removes the need: Focus already holds them. |
| A qualified external signing service for the contratante certificate | No connector needs signing by Axorim beyond the SERPRO session; the termo is signed by the firm outside Axorim. A broker container with secrets mounted only there keeps the exposure to one credential at lower cost. |
| Cache SERPRO tokens in Redis | Redis is shared by web and worker and is persisted; a token there outlives the broker and widens who can read it. |
| Retry registrations with idempotency keys | Neither provider documents idempotency for these events. A second Ciencia or termo is a second government event, so ambiguity is resolved by reading state. |

## Consequences

- ADR 0006's boundary still holds for clients: no client key material, ever. The only
  change is that status records about provider-held certificates are now allowed, and
  that Axorim's own contratante certificate exists as broker secrets.
- Every provider feature ships behind its per-tenant flag, off by default, with
  `PROVIDERS_PAUSED=True` by default and real-data calls refused unless
  `PROVIDER_REAL_DATA_APPROVED=True`.
- Leaving a provider is a configuration change plus a new adapter and a new stream
  epoch. Every captured artefact is stored as our own bytes with hashes, so evidence
  never points at a provider URL.
- Operator gates for secrets, contracts, termo and per-client authority (OG-R2 to OG-R6
  in `ops/AXORIM-R1-GO-NO-GO.md`) stay open until their evidence exists. This ADR closes
  none of them.
