# 0006: Digital certificates stay metadata-only in R1

- **Status**: Accepted
- **Date**: 2026-09-27
- **Gate**: AXR1-001 of the local-only plan `axorim-pivot-r1` (see `docs/project-status.md`)
- **Applies to**: every model, upload path and integration in the R1 release

## Question

Official Brazilian integrations need ICP-Brasil certificates. SERPRO's Integra Contador,
for example, authenticates with OAuth2 client credentials plus the contracting firm's
e-CNPJ certificate over mutual TLS, and NF-e distribution uses the taxpayer's or an
authorized party's certificate. Does R1 start holding certificates or private keys so
those integrations can follow?

## Decision

No. R1 keeps the boundary the code already has: the client registry stores whether a
client has a certificate and when it expires, and nothing else. The model comment at
`apps/clients/models/company.py:137-140` states it, and no field can hold the
certificate itself.

- No R1 table, file field, upload path or setting may hold a certificate, a PFX or P12
  file, a private key or a certificate password.
- R1 ships no government connector. Where an official document is needed (a DAS guide,
  for instance), staff upload it and it's labelled as manually supplied by staff, never
  as confirmed by the authority.
- When connectors come (R2 at the earliest), custody goes to one of two places, chosen
  by a separate owner decision:
  1. a qualified external signing or credential service, preferred; or
  2. an isolated broker process on a separate host, with keys wrapped by a key
     management service, a per-purpose allowlist and an access audit.

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| Store A1 certificates (PFX plus password) in the web application's database or bucket | Makes the product a holder of signing keys for every client of every firm. One application compromise becomes a signing compromise across tenants. Rejected outright, not just deferred. |
| Encrypt certificates in the web database with an application-held key | The key sits next to the ciphertext in the same process, so it's the option above with extra steps. |
| Build the isolated broker in R1 | Premature. No connector ships in R1, and the choice between an external service and a broker depends on contracts and costs that aren't settled. |

## Consequences

- R1 can't generate guides or read government mailboxes automatically. It records
  what staff and clients supply, labelled by source, and the status vocabulary
  (`docs/product/vocabulary.md`) never presents manual evidence as confirmed.
- Every R2 connector is blocked on the custody decision. That dependency is explicit,
  not discovered late.
- A guard test that fails when a client-scoped model gains a field able to hold key
  material is worth adding alongside the first R1 migration on the client registry. It
  is proposed here and described in words because it doesn't exist yet.
