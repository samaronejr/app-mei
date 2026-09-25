# Releases

## v0.2.0-rc1, pilot release candidate

**Status: prepared, not tagged.** No `v0.2.0-rc1` git tag exists and no GitHub release
exists. Creating them is the plan's closing operator action, executed only after the
four F-wave audits all return APPROVE and the owner explicitly approves. Preparing these
notes does not create the release, and the release does not start the pilot.

### Why the placeholders stay in this file forever

Two values cannot exist until after the audits: the owner's approval date, and the live
`/versionz` observation taken on the same day as the tag. They are written here as
post-wave placeholders with the exact command that fills each.

At Phase B the operator **renders a temporary copy** with those two substituted and tags
from that copy. **The tracked file is never edited.** That keeps the reviewed final HEAD
byte-identical to what the F-wave approved, which would not be true if tagging required
a commit. The two live values therefore appear in the GitHub release body and in
`.evidence/PILOT-505-operator.txt`, not here.

The placeholder marker is written in this document only on the two placeholder lines
themselves. Wherever a command below needs the marker, it is assembled from two quoted
fragments so that the document's own placeholder count stays at exactly two.

### Release fields

| Field | Value | Command that produces it |
| --- | --- | --- |
| **Source SHA** | `92cf94ae24bcdd962afb3b9bd5d5d27e520d3b1b`, the reviewed HEAD: `origin/main` when these fields were filled on 2026-09-25, and the release `/versionz` served that day (`.evidence/PILOT2-505-happy.txt`) | `git rev-parse main` |
| **Deployed SHA** | PENDING-F-WAVE, the live release observed the same day as the tag | `curl -fsS https://samaronefialho.dev/versionz` |
| **Migration state** | 99 applied, 0 pending, from deploy assert 2/6 on 2026-09-24 (`.evidence/deploy-76df579-operator.txt`). `76df579..92cf94a` adds no migration files, so the count holds at the reviewed HEAD | `docker compose -f docker-compose.prod.yml exec -T web python manage.py showmigrations --plan \| grep -c '^\[X\]'` |
| **Backup timestamp** | `20260924T060702Z` (`pg/20260924T060702Z/`), the first clean scheduled nightly on the target host after cutover; service exit 0 at 2026-09-24T06:07:16Z (`.evidence/PILOT2-406-operator.txt`) | the freshness marker written by `ops/backup.sh` |
| **Off-host timestamp** | 2026-09-24T06:07Z, off-host success for `pg/20260924T060702Z/logical.dump`. A separate reader ran `head-object` and a download at 2026-09-24T14:22:47Z; SHA-256 matched the local dump. Minute precision comes from the off-host success ping, since `LastModified` wasn't transcribed (`.evidence/PILOT2-406-operator.txt`) | `aws s3api head-object --bucket "$OFFHOST_S3_BUCKET" --key "pg/<stamp>/logical.dump" --query LastModified --output text` |
| **Restore rehearsal date** | 2026-09-21 (Track L) and 2026-09-22 (Track H), disposable workstation scratch at `c7afe36` (`.evidence/PILOT2-302L-operator.txt`, `.evidence/PILOT2-302H-operator.txt`) | recorded in `ops/RESTORE.md`, "Recovery timing status" |
| **Restore RTO** | Track L 6 min (310 s). Track H 98 min, with RPO 1.9275 h of dump age at recovery start | `ops/RESTORE.md` timing commands, `pilot-302-track-l-start.epoch` and `pilot-302-track-h-start.epoch` |
| **Rollback target** | image tag `app-mei:previous` plus `app-mei-caddy:previous`, with the restore procedure in `ops/RESTORE.md` | `docker image ls --filter reference="app-mei*:previous"` |
| **Runbook path** | `ops/PILOT-RUNBOOK.md` | `test -f ops/PILOT-RUNBOOK.md` |
| **Owner approval** | PENDING-F-WAVE, the date the owner records approval to tag | recorded in the release discussion and in `ops/PILOT-RUNBOOK.md`; the date is transcribed into `.evidence/PILOT-505-operator.txt` |

`PENDING-DEPLOY` and `PENDING-REHEARSAL` are **not** post-wave placeholders. They mark
values produced by work that has not run yet: the PR-5 merge and its deploy, PILOT-406's
post-cutover off-host verification, and PILOT-302's restore rehearsal. They are filled by
the command beside them when that work completes, and they are never filled from a desk
check, a local simulation, or an estimate. `ops/RESTORE.md` carries the same
`PENDING-REHEARSAL` discipline for the same reason.

### Known limitations

Shipped deliberately, in scope for a controlled pilot with one cooperative firm:

- No public signup. Accounts exist only by invitation.
- No government-portal automation. Filing remains a manual accountant workflow.
- No production data of any real firm is imported without written authorization.
- WAL is not shipped continuously off-host. Point-in-time recovery exists only while the
  host lives; total host loss recovers from the last nightly off-host logical dump.
- Object storage is a single bucket with versioning. No cross-region replication.
- Orphaned object bytes are detected and reported, never deleted automatically. Cleanup
  is a manual operator-verified procedure.
- A Redis outage returns site-wide HTTP 429 rather than 500. This is fail-closed by
  design, visible in Sentry, and signalled by `/readyz`.
- allauth verification and password-reset send failures surface as HTTP 500 on those
  POSTs. Enumeration-safe, Sentry-visible, operator procedure in the runbook.
- Pilot scale only: 10 to 30 MEI clients, one firm, one real monthly cycle.

### Residuals accepted for this release

The accepted-residual set governing this candidate, each carrying its anchor in
`docs/residual-risks.md` where one exists:

- Browser-history and email-scanner observation of URL bearer credentials. Consumption
  is disproven and pinned; observation is not repository-controllable.
- Per-endpoint 5/m rate limit on credential routes, 15/m combined per address.
- Redis outage produces a site-wide 429 availability mode; redis `TimeoutError` still
  produces a 500.
- Invitation lifecycle HTTP 410 disclosure for dead tokens. Deliberate.
- Sentry scrubbing is shape-dependent. Any `sentry-sdk` upgrade re-runs the
  credential-canary suite; this standing rule lives in `ops/PILOT-RUNBOOK.md`.
- allauth verification and reset send-failure 500s, accepted at pilot scale.
- Single-bucket object storage beyond versioning, and no continuous WAL shipping. The
  host-loss track is the off-host logical dump; recovery point is the last nightly dump.
- Data-subject-request notification volume is bounded per address and per source IP but
  not globally. Watched via the SES account dashboard during the pilot.
- Orphaned object bytes are reported, not deleted. Exposure is storage cost, not
  confidentiality.
- Stacked pull requests cannot merge under required checks.

### What this release is not

- It is **not** the start of the pilot. Pilot start is a separate, explicit owner
  approval, recorded separately. Tagging grants nothing.
- It is **not** a claim that the walkthrough, the drill, or the restore rehearsal has
  been performed. Their evidence artifacts are the only acceptable proof.

## Phase B, the post-audit operator step

Executed only after all four F-wave audits return APPROVE **and** the owner approves.
Nothing here runs during preparation.

```sh
# 1. The marker is assembled from two fragments so that this file's own
#    placeholder count stays exactly two. In the shell it is one string.
MARK='PENDING-F'-'WAVE'

# 2. Resolve the three values that must agree.
FINAL_HEAD=$(git rev-parse main)
LIVE_RELEASE=$(curl -fsS https://samaronefialho.dev/versionz | python3 -c 'import json,sys; print(json.load(sys.stdin)["release"])')
APPROVAL_DATE=$(date -u +%Y-%m-%d)   # the date the owner recorded approval

# 3. Render a TEMPORARY copy. The tracked file is not edited.
sed -e "s|${MARK}, the live release observed the same day as the tag|${LIVE_RELEASE}|" \
    -e "s|${MARK}, the date the owner records approval to tag|${APPROVAL_DATE}|" \
    ops/RELEASES.md > /tmp/release-notes-rc1.md

# 4. Three-way SHA equality. Refuse to tag unless all three agree.
DEPLOYED_HEAD=$(git rev-parse "$FINAL_HEAD")
test "$FINAL_HEAD" = "$LIVE_RELEASE" || { echo "REFUSE: main != live /versionz"; exit 1; }
test "$FINAL_HEAD" = "$DEPLOYED_HEAD" || { echo "REFUSE: tag target != main"; exit 1; }
echo "THREE-WAY SHA EQUALITY OK: $FINAL_HEAD"

# 5. Tag the final reviewed HEAD, then publish from the rendered copy.
git tag -a v0.2.0-rc1 "$FINAL_HEAD" -m "pilot release candidate"
git push origin v0.2.0-rc1
gh release create v0.2.0-rc1 --notes-file /tmp/release-notes-rc1.md

# 6. Evidence, no secrets.
{ echo "final head: $FINAL_HEAD"; echo "live release: $LIVE_RELEASE"; \
  echo "owner approval: $APPROVAL_DATE"; git tag; gh release list; } \
  >> .evidence/PILOT-505-operator.txt
```

If the three-way equality fails, **do not tag.** A tag pointing at a SHA that is not
what the box is serving makes the rollback anchor a lie.

A created tag can be deleted before announcement. After pilot start it cannot.

## Machine checks for these notes

Two checks gate this document. Both run against the tracked file.

1. **Placeholder count.** Exactly two post-wave placeholders exist. The command is
   recorded in `.evidence/PILOT-505-happy.txt` rather than here, because writing the
   marker literal a third time would break the property it measures.

2. **Field presence.** Every release field named above is present. One `grep -q` per
   field, single loop, nonzero exit naming the first missing field:

```sh
for f in "Source SHA" "Deployed SHA" "Migration state" "Backup timestamp" \
         "Off-host timestamp" "Restore rehearsal date" "Restore RTO" \
         "Rollback target" "Runbook path" "Owner approval" \
         "Known limitations" "Residuals accepted"; do
  grep -qE "^(\| \*\*|### )$f" ops/RELEASES.md || { echo "MISSING FIELD: $f"; exit 1; }
done
echo "FIELD PRESENCE OK"
```

   The pattern is anchored on the field's own **label** (a table row's bold cell, or a
   section heading) rather than on the bare name. Anchoring matters: this document
   quotes its own field list in the loop above, so an unanchored `grep` would find every
   field in that quoted list and pass even after the real field line had been deleted.
   The mutation exercise in `.evidence/PILOT-505-failure.txt` is what surfaced that, and
   the anchored form is the fix.
