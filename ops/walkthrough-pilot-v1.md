# Pilot walkthrough, identity table v1

A blank instrument. The operator fills it in **while driving** the 38 pilot-critical
steps against the target VPS. Nothing below records a walkthrough that happened: no
walkthrough has been run, no target VPS exists yet, and every PASS/FAIL cell and every
evidence cell is empty on purpose.

Fill one row at a time, in order, from a real browser with real mailboxes and a real
TOTP app. Transcribe each completed row into `.evidence/PILOT-501-happy.txt`; the six
negative controls also go to `.evidence/PILOT-501-failure.txt`.

## The governing rule

> **HTTP 200 alone never passes a row.**
>
> A row passes only when **every** identity field on it matches: host, URL, the single
> `h1` block text the page rendered, the authenticated account, the tenant, the client,
> and the `/versionz` release SHA observed for that page-load batch. A 200 with the
> wrong `h1`, or with a stale release SHA, or served to the wrong account, is a
> **FAIL**, and a FAIL blocks the release.

Two corollaries the operator must not soften:

1. **Negative rows pass by being refused.** Rows 19 through 23 and row 14 are the six
   negative controls. Their expected result is a refusal, and a 200 showing the data is
   the failure. They may not be skipped, deferred, or marked n/a.
2. **The release SHA is re-read per batch, not once.** Record `/versionz` at the start
   of each contiguous browsing batch (rows 1, 15, 18, 24, 31, 35 at minimum). A row
   whose batch SHA differs from the SHA at row 1 means a deploy landed mid-walkthrough,
   and every row already taken in that session is void.

## Hosts

| Symbol | Value |
| --- | --- |
| `FIRM` | `<pilot-slug>.samaronefialho.dev` |
| `PORTAL` | `<pilot-slug>-portal.samaronefialho.dev` |

The portal hostname is the firm slug plus the reserved portal suffix; the suffix is
required and stripped by `portal_slug_from_host` (`apps/portal/hosts.py`), and a host
without it is not a portal host. `<pilot-slug>` is chosen at tenant creation (row 3) and
written into the header of the filled copy.

## Where the expected `h1` values come from

`templates/base.html:136` emits the page's only `<h1>` structurally, as
`{% block heading %}` with the default `app-mei`; a guard test refuses a literal `<h1>`
in any other template. Three shells exist, and which one a URL uses decides its `h1`:

| Shell | `h1` emitted at | Default when a page overrides nothing |
| --- | --- | --- |
| Firm app shell, `templates/base.html` | `templates/base.html:136` | `app-mei` |
| allauth entrance shell, `templates/allauth/layouts/base.html` | `templates/allauth/layouts/base.html:71-72` | portal host: `Portal do cliente`; resolved firm: the tenant's `name`; neither: `app-mei` |
| Portal shell, `apps/portal/templates/portal/base.html` | `apps/portal/templates/portal/base.html:60` | `Portal do cliente` |

Every allauth screen (sign-in, sign-up, verification, password reset, the whole TOTP
enrolment tree) renders inside the entrance shell, which extends nothing, and therefore
takes its `h1` from the three-case expression at
`templates/allauth/layouts/base.html:72`. On the firm host with a resolved tenant that
expression yields the **tenant name**, which is why so many rows below expect
`<tenant-name>` rather than a screen-specific title.

Per-page overrides, each derived from the template that declares it. Dynamic values are
written as `<placeholder>` with the source of the value named; they are never invented:

| Source key | Expected `h1` | Derived from |
| --- | --- | --- |
| `H-DEFAULT` | `app-mei` | `templates/base.html:136` |
| `H-ENTRANCE` | `<tenant-name>` (the tenant's `name`, firm host with a resolved tenant) | `templates/allauth/layouts/base.html:72` |
| `H-ENTRANCE-PORTAL` | `Portal do cliente` | `templates/allauth/layouts/base.html:72`, portal-urlconf branch |
| `H-DASH` | `Painel` | `templates/core/dashboard.html:3` |
| `H-CLIENTS` | `Clientes` | `templates/clients/list.html:3` |
| `H-CLIENT` | `<client-legal-name>` (the client's `legal_name`) | `templates/clients/detail.html:3`, context set at `apps/clients/views.py:300` |
| `H-TEAM` | `Equipe` | `templates/accounts/team.html:3` |
| `H-INVITE-ISSUED` | `Convite enviado` | `templates/accounts/invite_issued.html:3` |
| `H-INVITE-ACCEPT` | `Convite para <tenant-name>` | `templates/accounts/invite_accept.html:3` |
| `H-INVITE-REFUSED` | `Convite indisponível` | `templates/accounts/invite_refused.html:3` |
| `H-INVITE-CONFIRM` | `Revogar convite` | `templates/accounts/invite_confirm.html:3` |
| `H-PORTAL-ACCEPT` | `Convite de <tenant-name>` | `templates/accounts/portal_invite_accept.html:3` |
| `H-PORTAL-HOME` | `<client-legal-name>` when a client resolves, else `Portal do cliente` | `apps/portal/templates/portal/home.html:50` |
| `H-PORTAL-PAY` | `Pagamentos` | `apps/portal/templates/portal/payments.html:47` |
| `H-PORTAL-DOCS` | `Documentos` | `apps/portal/templates/portal/documents.html:37` |
| `H-ADMIN-SELECT` | `Selecione uma empresa` | `templates/admin/select_tenant.html:3` |
| `H-QUEUE` | `<obligation-spec-label>` (the spec's `label`) | `templates/obligations/queue.html:3`, context set at `apps/obligations/views.py:293` |
| `n/a JSON` | no HTML, no `h1` | `apps/core/views.py:103-106` returns `JsonResponse({"release": ...})` |
| `n/a admin` | Django admin's own chrome, outside the three shells | `config/urls.py:24` |

`account_signup` has no repository override: the closed-signup screen renders allauth's
bundled `account/signup_closed.html`, which extends `account/base_entrance.html` and so
lands in the entrance shell. Its expected `h1` is therefore `H-ENTRANCE`, and the
template contract asserted elsewhere in the suite is the template **name**
`account/signup_closed.html`, not the heading.

## The 38 rows

Columns: **#** step number, **Step** what the operator does, **Host**, **URL**, **h1**
the expected heading text, **Src** the source key from the table above, **Account** the
authenticated identity, **Tenant**, **Client**, **SHA** the `/versionz` release observed
for this batch, **P/F**, **Evidence** the artifact reference.

`-` means the field does not apply to that row. Empty cells are for the operator.

| # | Step | Host | URL | h1 | Src | Account | Tenant | Client | SHA | P/F | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Read the release identity, this is the batch baseline | FIRM | `/versionz` | none, JSON body `{"release": "<40-hex>"}` | `n/a JSON` | anonymous | - | - | | | |
| 2 | Observe the TLS certificate, issuer plus notAfter | FIRM | `:443` handshake | - | - | anonymous | - | - | | | |
| 3 | Create the pilot firm (tenant) via admin | FIRM | `/admin/` then `/admin/selecionar-empresa/` | `Selecione uma empresa` on the selector | `H-ADMIN-SELECT` | operator (staff) | `<tenant-name>` | - | | | |
| 4 | Create the first client, then open its detail page | FIRM | `/clientes/` then `/clientes/<uuid>/` | `Clientes`, then `<client-legal-name>` | `H-CLIENTS`, `H-CLIENT` | operator | `<tenant-name>` | client A | | | |
| 5 | Issue a colleague invitation from the team screen | FIRM | `/equipe/` then `/convites/enviado/` | `Equipe`, then `Convite enviado` | `H-TEAM`, `H-INVITE-ISSUED` | operator | `<tenant-name>` | - | | | |
| 6 | Receive the firm invitation in the real mailbox, record message-id | mailbox | - | - | - | colleague mailbox | `<tenant-name>` | - | | | |
| 7 | Open the invitation link, acceptance screen renders | FIRM | `/convites/aceitar/<token>/` | `Convite para <tenant-name>` | `H-INVITE-ACCEPT` | anonymous holder of the token | `<tenant-name>` | - | | | |
| 8 | Complete acceptance, land on the mandatory verification screen | FIRM | `/accounts/confirm-email/...` | `<tenant-name>` | `H-ENTRANCE` | colleague | `<tenant-name>` | - | | | |
| 9 | Enrol TOTP in a real authenticator app | FIRM | `/accounts/2fa/totp/activate` | `<tenant-name>` | `H-ENTRANCE` | colleague | `<tenant-name>` | - | | | |
| 10 | Re-login with the TOTP code, second factor demanded | FIRM | `/accounts/login` then `/accounts/2fa/authenticate` | `<tenant-name>` | `H-ENTRANCE` | colleague | `<tenant-name>` | - | | | |
| 11 | Issue the portal invitation for client A's owner | FIRM | `/clientes/<uuid>/` | `<client-legal-name>` | `H-CLIENT` | operator | `<tenant-name>` | client A | | | |
| 12 | Receive the portal invitation in the second real mailbox | mailbox | - | - | - | portal mailbox | `<tenant-name>` | client A | | | |
| 13 | Accept the portal invitation on the portal host | PORTAL | `/convites/aceitar/<token>/` | `Convite de <tenant-name>` | `H-PORTAL-ACCEPT` | anonymous holder of the token | `<tenant-name>` | client A | | | |
| 14 | **Negative control 1**, public signup is closed | FIRM | `/accounts/signup` | `<tenant-name>`, body is the closed screen, template `account/signup_closed.html` | `H-ENTRANCE` | anonymous | `<tenant-name>` | - | | | |
| 15 | Portal home as the client owner | PORTAL | `/` | `<client-legal-name>` | `H-PORTAL-HOME` | portal user | `<tenant-name>` | client A | | | |
| 16 | Portal Pagamentos | PORTAL | `/pagamentos/` | `Pagamentos` | `H-PORTAL-PAY` | portal user | `<tenant-name>` | client A | | | |
| 17 | Portal Documentos | PORTAL | `/documentos/` | `Documentos` | `H-PORTAL-DOCS` | portal user | `<tenant-name>` | client A | | | |
| 18 | Upload the canary document through the real object-storage backend | PORTAL | `POST /documentos/enviar` | `Documentos` on return | `H-PORTAL-DOCS` | portal user | `<tenant-name>` | client A | | | |
| 19 | Download the canary and verify its `sha256` matches the uploaded bytes | PORTAL | `/documentos/<storage_key>` | - | - | portal user | `<tenant-name>` | client A | | | |
| 20 | Re-record the canary `storage_key` and `sha256` for rows 35 to 38 | PORTAL | `/documentos/` | `Documentos` | `H-PORTAL-DOCS` | portal user | `<tenant-name>` | client A | | | |
| 21 | **Negative control 2**, anonymous fetch of the canary object is refused | PORTAL | `/documentos/<storage_key>` signed out, plus the bucket URL direct | expect refusal, no bytes | - | anonymous | - | client A | | | |
| 22 | **Negative control 3**, second client's portal user cannot see client A's document | PORTAL | `/documentos/` and `/documentos/<A-storage_key>` | `Documentos`, A absent; direct fetch refused | `H-PORTAL-DOCS` | portal user B | `<tenant-name>` | client B | | | |
| 23 | **Negative control 4**, second tenant sees nothing of tenant 1 | second FIRM/PORTAL | `/clientes/` and `/documentos/` | `Clientes` and `Documentos`, tenant 1 rows absent, direct fetch refused | `H-CLIENTS`, `H-PORTAL-DOCS` | tenant 2 user | `<tenant-2-name>` | - | | | |
| 24 | Firm Clientes registry lists exactly the provisioned clients | FIRM | `/clientes/` | `Clientes` | `H-CLIENTS` | operator | `<tenant-name>` | - | | | |
| 25 | Assigned staff accountant opens the client they are assigned to | FIRM | `/clientes/<uuid>/` | `<client-legal-name>` | `H-CLIENT` | assigned accountant | `<tenant-name>` | client A | | | |
| 26 | **Negative control 5**, unassigned staff accountant is refused that client | FIRM | `/clientes/<uuid>/` | expect refusal, not the client page | - | unassigned accountant | `<tenant-name>` | client A | | | |
| 27 | Password reset end to end through the real mailbox | FIRM | `/accounts/password/reset` then the emailed key URL | `<tenant-name>` | `H-ENTRANCE` | colleague | `<tenant-name>` | - | | | |
| 28 | Revoke the unused colleague invitation | FIRM | `/convites/<uuid>/revogar/` | `Revogar convite` | `H-INVITE-CONFIRM` | operator | `<tenant-name>` | - | | | |
| 29 | **Negative control 6**, the revoked token is dead, lifecycle page not the acceptance page | FIRM | `/convites/aceitar/<revoked-token>/` | `Convite indisponível`, HTTP 410 | `H-INVITE-REFUSED` | anonymous holder of the token | `<tenant-name>` | - | | | |
| 30 | Credential canaries absent from container logs across the whole window, with a positive-control grep proving the grep works | host shell | `docker compose logs` over rows 1 to 29 | - | - | operator on the host | - | - | | | |
| 31 | Drill (PILOT-504) part a, deliberate Sentry event arrives scrubbed, release field equals the deployed SHA | Sentry | - | - | - | operator | - | - | | | |
| 32 | Drill part b, stop beat, `/healthz` flips degraded inside 20 minutes and the monitor alert fires | FIRM | `/healthz` | none, JSON | `n/a JSON` | anonymous | - | - | | | |
| 33 | Drill part c, unrelated workflow survives the degraded window, one firm page still serves with correct identity | FIRM | `/clientes/<uuid>/` | `<client-legal-name>` | `H-CLIENT` | operator | `<tenant-name>` | client A | | | |
| 34 | Drill part d, start beat, recovery inside 10 minutes, monitor resolves | FIRM | `/healthz` | none, JSON | `n/a JSON` | anonymous | - | - | | | |
| 35 | Post-restore, fresh firm-side read against the restored data | FIRM | `/clientes/` | `Clientes` | `H-CLIENTS` | operator | `<tenant-name>` | - | | | |
| 36 | Post-restore, fresh firm-side client identity read | FIRM | `/clientes/<uuid>/` | `<client-legal-name>` | `H-CLIENT` | operator | `<tenant-name>` | client A | | | |
| 37 | Post-restore, fresh portal read | PORTAL | `/` then `/documentos/` | `<client-legal-name>`, then `Documentos` | `H-PORTAL-HOME`, `H-PORTAL-DOCS` | portal user | `<tenant-name>` | client A | | | |
| 38 | Post-restore, canary document hash re-check equals the row 19 and 20 value | PORTAL | `/documentos/<storage_key>` | - | - | portal user | `<tenant-name>` | client A | | | |

### The six negative controls, restated so they cannot be lost in the table

| Control | Row | Expected refusal |
| --- | --- | --- |
| Public signup closed | 14 | the closed-signup screen, `account/signup_closed.html`, never a usable signup form |
| Anonymous object fetch | 21 | refused, zero bytes returned, on both the app route and the bucket URL |
| Cross-client | 22 | client B's portal user never sees or fetches client A's document |
| Cross-tenant | 23 | tenant 2 sees no client, no document, no row of tenant 1 |
| Unassigned staff accountant | 26 | refused the client detail page they hold no assignment for |
| Revoked invitation token | 29 | HTTP 410 lifecycle page, `Convite indisponível`, never the acceptance form |

Skipping any of these voids the walkthrough.

## What the operator records per row

- The literal `h1` text as rendered, copied from the page, not retyped from this file.
- The `/versionz` release SHA for the batch.
- The account, tenant and client actually authenticated, not the ones intended.
- PASS or FAIL. No third value. A row that could not be attempted is a FAIL with the
  reason written in the evidence cell.

## Rows that cannot be filled yet

| Rows | Blocked on | State of the blocker |
| --- | --- | --- |
| 31 to 34 | PILOT-504, the Sentry plus degraded-signal drill | **not run.** These four rows are observed live during that drill and cannot be filled before it. |
| 35 to 38 | PILOT-302 (restore rehearsal), PILOT-303 (document bytes), PILOT-305 | **not run.** These rows tie to that evidence plus one fresh post-restore read each. |
| all rows | the target VPS, real mailboxes, a real TOTP app, the object-storage bucket | **not provisioned.** Waves 0 to 4 must land first. |

Nothing in this file may be filled from a desk check, a local simulation, or a
development stack. A value that did not come from the target host is not evidence.

## Reviewer control for this table

The identity discipline is itself checked: before the walkthrough is accepted, one row
in a **scratch copy** of this file is deliberately given a wrong expected `h1`, the
reviewer is asked to validate the walkthrough, and the wrong row must be caught. Then
the scratch copy is discarded. The tracked file is never mutated for this exercise.
