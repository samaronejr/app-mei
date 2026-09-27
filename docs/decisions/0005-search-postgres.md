# 0005: Search runs in PostgreSQL with `unaccent` and `pg_trgm`

- **Status**: Accepted
- **Date**: 2026-09-27
- **Gate**: AXR1-001 of the local-only plan `axorim-pivot-r1` (see `docs/project-status.md`)
- **Applies to**: the header search and the `Ctrl/Cmd+K` command palette added in R1

## Question

Staff need one search box that finds clients, cycles, requests and documents by name,
CNPJ or title, tolerant of missing accents and small typos, for firms of up to about
a thousand clients. Where does that search run?

## Decision

In PostgreSQL, over the same RLS-scoped querysets every other view uses.

- The service takes the actor, the query and the kinds to search, and returns at most
  five hits per kind.
- Matching uses the `unaccent` and `pg_trgm` extensions. Both ship with standard
  PostgreSQL 16; no new service is involved.
- The same search backs a visible header field and the keyboard palette, so nobody
  depends on the shortcut.

Extensions need the database superuser. `app_migrator` can create objects in the
`public` schema but has no `CREATE` on the database (`ops/sql/roles.sql:84-86`), and
every merge to `main` deploys to the pilot host. So:

- `ops/sql/roles.sql` gains `CREATE EXTENSION IF NOT EXISTS` for `unaccent`, `pg_trgm`
  and `btree_gist` (the last one is for effective-dated tax profiles).
- The live pilot database gets the same statements from a one-time runbook step,
  run by the operator **before** the first PR that needs them merges.
- pytest builds each test database from `template1`, so CI and local bootstraps create
  the extensions in `template1` too.
- Migrations only assert that the extensions exist and fail with a clear message if
  they don't. They never try to create them.

## Alternatives considered

| Option | Why it was not taken |
| --- | --- |
| A search engine (Elasticsearch, OpenSearch, Meilisearch, Typesense) | New infrastructure to run and pay for, an index that sits outside RLS, and a second copy of client data whose tenant isolation would have to be proven all over again. |
| A vector store or embedding search | Out of scope for R1, and it would send or store document text in a new place. |
| Plain `icontains` lookups | No accent folding (`Joao` would miss `João`) and no typo tolerance. |
| PostgreSQL full-text search (`tsvector`) alone | Good for long text, weak for CNPJs, short names and partial matches, which are what staff actually type. It can be added later next to trigrams if document text search is ever needed. |

## Consequences

- Search can't leak across tenants or clients through a separate index, because there
  is no separate index. Isolation tests that cover the querysets cover search.
- One operator step (creating the extensions on the live database) must happen before
  the first PR that relies on them merges. It's recorded as an open operator gate until
  its evidence exists.
- Revisit if measured search latency on the thousand-client synthetic tenant misses its
  budget with trigram indexes in place.
