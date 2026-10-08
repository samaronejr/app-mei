# External services and the data they touch (engineering list)

This is the technical inventory of every external service the running product talks
to, and the categories of data each one can see. It's written for engineers and for
the professional review of the legal texts (OG-08). It isn't a legal subprocessor
list, and it isn't a contract. The customer-facing list, the terms of use and the
operator DPA are drafts kept outside this repository until that review is done.

Sources: `ops/README.md` (hosting, storage, email, monitoring runbooks) and
`config/settings/prod.py` (storage and Sentry configuration). When a provider changes
there, this list changes in the same pull request.

## Services that process customer data

| Service | What it does here | Data categories it can see | Location noted in the runbooks |
| --- | --- | --- | --- |
| AWS Lightsail | Serving host: web app, PostgreSQL, Redis, background workers, local backups | Everything the product stores: firm accounts, staff and client users, client companies (CNPJ, names, contacts), cycles, requests, messages, document metadata, audit and access logs. Uploaded document bytes pass through it on upload and download. | Lightsail instance behind `samaronefialho.dev` since 2026-09-22 |
| Oracle Cloud Infrastructure (OCI) Object Storage | Documents bucket, through the S3-compatible API (`OCI_S3_ENDPOINT_URL`); off-host copies of the nightly database backup | Uploaded document bytes (which can hold employee payroll data and counterparty invoice data), quarantine and export prefixes once OG-04 closes, and full database backups | OCI bucket; region set by the operator in the environment |
| Resend (relaying through Amazon SES) | Outbound email over SMTP: invitations, password resets, requests and messages to clients | Recipient addresses, sender identity, subject and body of each message, and the provider's response | Resend domain verified in `sa-east-1`; delivery relayed through Amazon SES `sa-east-1` |
| Sentry | Error and performance events from the web app and workers | Stack traces and request context after scrubbing: passwords, codes, request bodies and credential URLs are filtered before sending (`config/settings/prod.py`) | Sentry project set by `SENTRY_DSN` |

## Services that see no customer records

| Service | What it does here | Data categories it can see |
| --- | --- | --- |
| Healthchecks.io | Dead-man signals for the scheduler, the nightly backup and the off-host copy | Check pings: timestamps and exit status. No customer data. |
| UptimeRobot | Keyword monitors against public health endpoints | Response status and the health keywords of `/healthz`. No customer data. |
| Cloudflare | DNS for the domain and inbound email routing for addresses at the domain | DNS queries; mail sent to addresses at the domain (for example the LGPD channel), never product data |
| GitHub | Source code, CI and the deploy pipeline | Code and synthetic test data only. Production data never goes to CI. |

## Not in use

- **AI providers.** No customer data goes to any model provider. The R1 evaluation
  harness and shadow runner work on synthetic data only, and a real provider stays
  off until OD-05 (a DPA with no-training and retention terms) closes.
- **Payment processors.** Billing is manual (OD-04). No card or bank data is stored.
- **Malware scanning** runs as a ClamAV sidecar on the serving host (OG-05), not as an
  external service.

## Data minimization notes for the review

- Documents uploaded by clients can contain personal data of people who aren't users:
  the client's employees (payroll) and counterparties (invoices). The product stores the
  bytes and the hash, and indexes metadata only. It doesn't extract or search those
  people's data.
- Message bodies sent through Resend are the texts staff approved. Staff should keep
  personal data of third parties out of message text and attach documents instead,
  which stay in the documents bucket behind authentication.
- Retention per data class is documented in `docs/retention.md`. The purge runs as a
  dry run until OG-08 closes.
