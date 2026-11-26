# ISS retention party: rollout and code-revert recovery

Before applying obligations `0030` and `0031` to a populated register, pause
and drain fiscal capture and imports and pause retention purges. The data
migration reads the scoped, still-retained originals from the configured
private vault; it never calls the capture provider. Run migrations with the
migration database role and vault access. Wait for `0031` to finish before
enabling readers that use `iss_retention_party`.

For a code-only revert, disable `document_capture` first and leave the schema
in place. The old writer can resume with `0030` still applied, but any fiscal
identity it creates **or advances** may have a default or stale party. Before
re-enabling readers of that party with the repaired code, pause and drain
the writers and purges again, apply any pending migrations, then run:

```sh
DATABASE_URL="$DATABASE_MIGRATION_URL" python manage.py backfill_iss_retention_party
```

Run this in the configured application environment with the private vault
available. The command examines all current heads, not just rows marked
`desconhecido`. An unavailable or uncorroborated original does not establish
a party. Retry a failed run before enabling readers; a repeat on unchanged
evidence updates no rows. Neither migration nor catch-up rewrites established
historical flags or amounts. On recovery from an evidence outage, maintenance
uses the writer's ownership rule: it may refresh a writer-owned current
revision, or complete an unfinished projection with both old columns unset.
An older outage flag alone does not authorize changing an existing legacy
tuple. Retrying through live ingest gives the same result. Neither path clears
an incident caused by contradictory evidence.

The `0031` reverse does not erase audit or exception history. If the vault is
unavailable during the pass, affected rows can remain uncertain with an owned
exception. Run catch-up again after restoring vault access.
