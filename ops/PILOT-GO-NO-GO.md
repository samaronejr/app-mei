# Pilot go/no-go ledger

The controlled pilot is **GO** only when this command exits 0, run from a clean checkout
of `origin/main`:

```sh
python3 ops/check_go_no_go.py ops/PILOT-GO-NO-GO.md "$EVR"
```

`$EVR` is the untracked evidence directory (`.evidence/` in the execution worktree).
Exit 0 means all 21 Success-criteria lines of `pilot-readiness-v2` are GO. Exit 1 names
every NO-GO line and the reason. After that, the F-wave audits still have to APPROVE and
the owner still has to authorize the start. Exit 0 grants nothing on its own.

## This file holds structure, never observations

No SHA, date, or verdict is written here. Observations live in `$EVR` and are read at
check time. That's deliberate: if merging this file recorded an observation, the merge
itself would move `main` and make the observation stale. Nobody fills this file in. If a
line is NO-GO, fix the evidence or the system, never the ledger.

## How a row is read

- **line** names the Success-criteria line. A row whose first cell is empty continues the
  line above it, with another evidence file. Every row of a line must pass.
- **kind** is `final-release` or `drill`.
  - `final-release` rows read `$EVR/PILOT2-506-final.txt`, the final capture. It is
    taken after the last merge to `main`, and every SHA in it has to be the same SHA.
  - `drill` rows read evidence recorded when a procedure ran. They prove the
    procedure and its effect. The SHA it ran at (`head_sha=` or `drill_sha=`) is
    historical: the checker prints it and never compares it with the release.
- **evidence_file** is the one file in `$EVR` that must hold the row's literals. An
  absent file is NO-GO. A line is never GO without its evidence.
- **expected** lists the literals. One is present when a line of the file starts with
  it and is followed by the end of the line, a space, or `;`. So `journey1=PASS` does
  not match `journey1=PASSED`. A literal ending in `=` requires only the key. Its value
  is judged by the final-release rule.
- **rule** is the verdict rule in words, with what the literals cannot express.

The final-release rule, applied to every `final-release` row: `final_main_sha`,
`unpinned_versionz`, `lightsail_versionz`, `ci_push_head_sha` and
`verify_live_head_sha` each appear exactly once, are 40-hex, and all equal
`git rev-parse origin/main` at check time. `capture_utc` is not earlier than that
commit. No value in the capture reads `PENDING` or `UNVERIFIED`. One changed hex digit
anywhere makes S1, S2, S3, S18 and S20 NO-GO together.

The checker also holds the plan's split in place. S1, S2, S3 and S20 open with a
`final-release` row. S18 has a `final-release` row. Every other line is drill-only. Each
S-line appears exactly once.

## Ledger

| line | kind | evidence_file | expected | rule |
| --- | --- | --- | --- | --- |
| S1 | final-release | `PILOT2-506-final.txt` | `final_main_sha=` `unpinned_versionz=` `lightsail_versionz=` `capture_utc=` `dns_apex=18.229.187.66` `oci_resolve_probe=down` | Same-day triple: main, the unpinned `/versionz` and the Lightsail `--resolve` `/versionz` name one SHA in one capture; public DNS points at Lightsail; the old OCI address gives no HTTP answer. |
| S2 | final-release | `PILOT2-506-final.txt` | `ci_push_head_sha=` `ci_push_last=success` `ci_push_asserts=6/6` `ci_push_storage_probe=PASS` | The last push `ci.yml` run on main ran at the final SHA and succeeded, with all six deploy asserts and a clean storage probe on the target. |
| | drill | `PILOT2-003-happy.txt` | `mergeStateStatus=BLOCKED` `mergeStateStatus=CLEAN` `deploy_job_ran_on_pr=NO` | Branch protection proven by effect (row 4): red required check blocked the merge, green allowed it. |
| S3 | final-release | `PILOT2-506-final.txt` | `verify_live_head_sha=` `verify_live_last=success` `hc_verify_state=Up` | The last scheduled `verify-live` run ran at the final SHA, so after this ledger merged, and succeeded; its Healthchecks check reads Up. |
| S4 | drill | `PILOT2-106O-operator.txt` | `journey1=PASS` `journey2=PASS` `journey3=PASS` `journey4=PASS` `journey5=PASS` `journey6=PASS` `auth_results=spf=pass,dkim=pass,dmarc=pass` `mailboxes_are_distinct_external_inboxes=yes` | Six real-mailbox journeys from the target passed with aligned authentication (row 31). |
| | drill | `PILOT2-106-operator.txt` | `sendtestemail_exit=0` `received=yes` `auth_results_spf=pass` `auth_results_dkim=pass` `auth_results_dmarc=pass` `wrong_password_exit=1` | Pre-flip `sendtestemail` from the target was received and a wrong password was refused (row 20). |
| S5 | drill | `PILOT2-106O-operator.txt` | `wrong_smtp_ui=truthful-error` `wrong_smtp_invite_rows_delta=0` `wrong_smtp_sentry_event=yes` `dsr_encarregado_notified_at=set` `smtp_restored=yes` | A wrong SMTP credential gave a truthful UI error, created no Invite row, and raised a Sentry event (row 31). The DSR unset-then-set-on-retry semantics are pinned by `tests/accounts/test_invite_send_failure.py` and `tests/audit/test_lgpd_intake.py`, which run in the S2 CI. |
| S6 | drill | `PILOT2-104-operator.txt` | `dkim_txt_present=yes` `send_spf_present=yes` `apex_dmarc_unchanged=yes` `nxdomain_control=NXDOMAIN` `resend_domain_status=Verified` | DKIM, SPF and DMARC live, with the NXDOMAIN control (row 9). |
| | drill | `PILOT2-105-operator.txt` | `auth_results_dmarc=pass` `wrong_password_status=535` | Authenticated send passes DMARC; a wrong password is refused (row 10). |
| | drill | `PILOT2-106O-operator.txt` | `bounce_dashboard_status=Bounced` `suppression_procedure=executed` | A bounce was handled per the runbook (row 31). |
| S7 | drill | `PILOT2-405-operator.txt` | `PASS write` `PASS authenticated-read` `PASS anonymous-direct-refusal:` `PASS storage-url-anonymous-refusal` `PASS delete-and-confirm-gone` `wrong_credential_exit=1` `versioning=Enabled` `lifecycle_days=30` `lifecycle_enabled=yes` `restored_sha_equals_A=yes` | Real probe with a wrong-credential FAIL, versioning plus a 30-day lifecycle, byte-equal version restore (row 14). |
| | drill | `PILOT2-405M-operator.txt` | `skipped=ruling_iii=keep-oci` | Row 15 did not run, because the documents bucket stayed on OCI. |
| | drill | `PILOT2-403-happy.txt` | `storage_probe=PASS` | The same probe passed from the Lightsail serving host (row 18). |
| S8 | drill | `PILOT2-303-operator.txt` | `document_tenant=a` `document_tenant=b` `document_hash_match=yes` `reconcile_deleted=0` `nonexistent_key=NoSuchKey` | Restored-DB hashes equal live bytes for two tenants; reconcile deleted nothing; a nonexistent key is refused (row 24). |
| S9 | drill | `PILOT-201-failure.txt` | `status=503` | `/readyz` returns 503 on dependency loss (v1 evidence, still the current contract). |
| | drill | `PILOT2-504-699f764-operator.txt` | `healthz_status=503` `healthz_degraded_body=status-degraded` `recovered_healthz_status=200` | `/healthz` went 503 on a stale scheduler, observed live, then recovered (row 32). |
| | drill | `PILOT2-403-happy.txt` | `versionz_cache_control=no-store` | `/versionz` is served with `Cache-Control: no-store` (row 18). |
| | drill | `PILOT2-012E-happy.txt` | `4 passed` `compose_config_exit=0` | The cold-start deadlock fix is pinned by its tests (row 40). |
| S10 | drill | `PILOT2-406-operator.txt` | `monitors_up=M1a,M1b,M1c,M1d` `monitor_control_down=M1n` `new_monitor_states=M2:Up;M3:Up;M4:Up` | Keyword monitors UP against the target and the impossible-keyword control DOWN (row 30). |
| | drill | `PILOT2-504-699f764-operator.txt` | `monitors_down=M1a,M1b,M1c,M1d` `monitor_up=none` `firm_page_during_outage=200` `alert_acknowledged_by=Owner-on-call` `recovery_notification=yes` | The beat-stop drill flipped the monitors, the alert reached a human role who acknowledged it, and serving continued (row 32). S10 as written expects M1c UP. The evidence records M1c DOWN on HTTP 503 under an owner-approved amendment, with serving continuity shown by `firm_page_during_outage=200`. F4 rules on that amendment. |
| S11 | drill | `PILOT2-406-operator.txt` | `following_target_nightly_local_start_seen=yes` `following_target_nightly_local_success_seen=yes` `following_target_nightly_offhost_success_seen=yes` `following_target_nightly_service_exit=0` | A real scheduled nightly on the target signalled start and success (row 30). |
| | drill | `PILOT2-208-operator.txt` | `injected_offhost_failure_ping=fail` `injected_failure_exit=5` `restore_success_seen=yes` | An injected off-host failure was signalled and recovered (row 13). |
| | drill | `PILOT2-504-699f764-operator.txt` | `backup_missed_down=yes` `backup_missed_notification=yes` `backup_recovery_up=yes` `backup_alert_acknowledged_by=Owner-on-call` `backup_timer_final=enabled-active` | A missed run on the target alerted a human and recovered, with the timer re-armed (row 32). |
| S12 | drill | `PILOT2-301-operator.txt` | `bucket_versioning=Enabled` `bucket_sse=AES256` `offhost_logical_version_present=yes` `offhost_logical_sha_match=yes` `independent_read_host=operator-workstation` `reader_list=AccessDenied` `writer_delete=AccessDenied` `overwrite_prior_version_recoverable=yes` | Versioning and SSE-S3 verified; independent-host SHA equality; the reader cannot list; the writer cannot delete; an overwrite is recoverable (row 12). |
| | drill | `PILOT2-406-operator.txt` | `offhost_head_version_id=present` `offhost_head_sse=AES256` `offhost_sha_match=yes` | The target's nightly object carries a `VersionId` and matches by SHA (row 30). |
| S13 | drill | `PILOT2-302L-operator.txt` | `track=L` `row_status=closed` `restore_ready=yes` `roles_ok=yes` `migrations_pending=0` `rls_by_effect=t/t/t` `known_rows=t\|t\|t\|t` `non_exempt_rls_gap_rows=0` `complete_policy_inventory_match=yes` | Track L PITR restored with roles and RLS by effect; the known-rows result is the vacuity-proof pair (row 22). |
| S14 | drill | `PILOT2-302H-operator.txt` | `track=H` `offhost_sha_match=yes` `reader_credential_source=Proton` `corrupted_copy_pg_restore_list=FAIL` `totp_login=ok` `recovery_ready=yes` `rpo_hours=1.927500` `rto_minutes=98` `observer_utc_delta_seconds=` | Host-loss recovery from the off-host dump through the reader credential, with the corrupted-copy control FAIL and a TOTP login (row 23). The bounds are `rpo_hours <= 26` and `rto_minutes <= 240`. The recorded values are pinned here, so any edit to the drill record turns this line red. `ops/RESTORE.md` carrying those numbers is read at the final SHA under S20. |
| S15 | drill | `PILOT2-305-operator.txt` | `row1=PASS` `row2=PASS` `row3=PASS` `row4=PASS` `row5=PASS` `row6=PASS` `control1_entitled_page=200` `control1_refusal=404` `control2_cross_tenant_direct=404` `foreign_data_exposure=none-observed` | Restored-environment walkthrough 6/6 with both negatives, each preceded by its entitled page (row 25). |
| S16 | drill | `PILOT2-209-operator.txt` | `flood_mib=300` `rotated_file_count=5` `app_containers_logconfig=all-50m-5` `journald_systemmaxuse=500M` `invalid_config_rejected=yes` `canary_positive_control=user_agent_marker_hit=1` `canary_absent_control=0` | Rotation bounds held after a flood above 250 MB; journald capped; invalid config rejected; canary absent with a positive control (row 19). |
| | drill | `PILOT2-106O-operator.txt` | `sweep_positive_control_web=1` `sweep_absent_control=0` `sweep_token_hits=0` | The credential sweep found nothing, with a positive control (row 31). |
| | drill | `PILOT2-402-operator.txt` | `swap_mib=2111` `env_audit=OK` | Swap present; env audit OK (row 16). |
| S17 | drill | `PILOT2-402-operator.txt` | `row_status=complete` `required_nonempty=PASS` `env_audit=OK` | Phase B complete with zero placeholders (row 16). |
| | drill | `PILOT2-403-happy.txt` | `run_conclusion=success` `assert_6_6=ok` `runner_rule_absent=yes` `dns_apex=144.33.16.182` | Shadow deploy of main to Lightsail passed its asserts while DNS still pointed at the old host (row 18). |
| | drill | `PILOT2-404-operator.txt` | `transfer_sha_match=3/3` `row_counts_match=3/3` `canary_document_sha_match=yes` `pre_flip_readyz=ready` `post_step8_healthz=ok` `control2_altered_count_detected=yes` | Cutover steps 1-8 with row-count and canary checks, and the altered-count control 2 (row 28). |
| | drill | `PILOT2-404O2-operator.txt` | `dns_apex=18.229.187.66` `unpinned_readyz=ready` `control1_pinned_old_readyz=ready` `ttl_restored=300` `old_box_volumes_present=yes` | Steps 9-12: DNS flip, unpinned verification, rollback control 1 on the intact old box, TTL restored (row 29). |
| | drill | `PILOT2-401-operator.txt` | `oci_instance_state=STOPPED` `oci_volumes_intact=yes` `oci_public_ingress=none` | The old box is stopped with its volumes intact (row 38, Part 1 only). |
| S18 | drill | `PILOT2-501-699f764-operator.txt` | `row1=PASS` `row2=PASS` `row3=PASS` `row4=PASS` `row5=PASS` `row6=PASS` `row7=PASS` `row8=PASS` `row9=PASS` `row10=PASS` `row11=PASS` `row12=PASS` `row13=PASS` `row14=PASS` `row15=PASS` `row16=PASS` `row17=PASS` `row18=PASS` `row19=PASS` `row20=PASS` `row21=PASS` `row22=PASS` `row23=PASS` `row24=PASS` `row25=PASS` `row26=PASS` `row27=PASS` `row28=PASS` `row29=PASS` `row30=PASS` `row31=PASS` `row32=PASS` `row33=PASS` `row34=PASS` `row35=PASS` `row36=PASS` `row37=PASS` `row38=PASS` `negative14=PASS` `negative21=PASS` `negative22=PASS` `negative23=PASS` `negative26=PASS` `negative29=PASS` `write_negative_cross_tenant=refused` `csrf_negative=403` `document_sha_match=yes` `row38_canary_sha_match=yes` `versionz=` | Walkthrough 38/38 with six negatives and two write-side negatives, identity recorded per row (row 34). Its `versionz` is the historical walkthrough SHA. |
| | drill | `PILOT2-503-happy.txt` | `stop_conditions_occurred=0` `negative_control_result=PASS:renamed` | Metrics and stop conditions wired, with the missing-source control (row 36). |
| | final-release | `PILOT2-506-final.txt` | `walkthrough_code_diff=empty` `runbook_structure=pass` | Nothing that runs in the served app changed between the walkthrough SHA and the final SHA; otherwise the walkthrough is re-run. The runbook's roles and incident mapping hold their structure at the final SHA (row 35). |
| S19 | drill | `PILOT2-505-happy.txt` | `versionz_matches_origin_main=yes` `PLACEHOLDER COUNT OK` `FIELD TABLE FILLED OK` `FIELD PRESENCE OK` `PROSE UNCHANGED OK` | Release fields filled, exactly two F-wave placeholders left, and the three-way tag condition of `ops/RELEASES.md` ready (row 37). |
| S20 | final-release | `PILOT2-506-final.txt` | `status_declares_v2=yes` `status_cutover_record=yes` `status_resend_superseded=yes` `status_links_ledger=yes` `restore_md_measured=yes` | Truth documents at the final SHA: `docs/project-status.md` declares v2, keeps the cutover record, marks the Resend amendment superseded and links this ledger; `ops/RESTORE.md` carries the measured RTO. Whether the prose is current (expected-red jobs, decommission schedule, residual-risk table) is F4's call; the literals only prove the anchors are there. |
| S21 | drill | `PILOT2-000L-operator.txt` | `engagement_signed=yes` `engagement_date=` `encarregado_named=yes` `encarregado_public_contact=` `business_hours=` `on_call_ack_expectation=` `pilot_exit_commitment=` `spend_ceiling_usd_month=40` | The pilot-firm engagement is recorded (row 41). `engagement_signed=no` leaves this line NO-GO. |

Row 38 Part 2 (termination of the old host) is deliberately not an S-line. It runs after
the tag and is audited outside this ledger.

## The final capture

`$EVR/PILOT2-506-final.txt` is untracked. It's written once, after the last merge to
`main`, once the push `ci.yml` run for that merge has finished and deployed, and after
the first scheduled `verify-live` run on that SHA (the workflow runs daily at 08:17
UTC). Captured any earlier, it records an in-progress run or the previous release, and
the checker says NO-GO. Run the block below from the execution worktree, in a clean
checkout of `origin/main`, and don't edit the output by hand. A failed probe records a
non-matching value and the checker turns it into NO-GO.

```sh
set -u
git fetch origin && git switch --detach origin/main
test -z "$(git status --porcelain --untracked-files=no)" || { echo "dirty tree"; exit 1; }
FINAL=$(git rev-parse origin/main)
OUT="$EVR/PILOT2-506-final.txt"
release() { python3 -c 'import json,sys; print(json.load(sys.stdin)["release"])'; }
yes_no() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }
CI_RUN=$(gh run list --workflow ci.yml --branch main --event push --limit 1 --json databaseId --jq '.[0].databaseId')
CI_LOG=$(gh run view "$CI_RUN" --log)
WALK=$(sed -n 's/^head_sha=//p' "$EVR/PILOT2-501-699f764-operator.txt")
{
  echo "capture_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "final_main_sha=$FINAL"
  echo "dns_apex=$(dig +short samaronefialho.dev A | tail -n1)"
  echo "unpinned_versionz=$(curl -fsS --noproxy '*' https://samaronefialho.dev/versionz | release)"
  echo "lightsail_versionz=$(curl -fsS --resolve samaronefialho.dev:443:18.229.187.66 https://samaronefialho.dev/versionz | release)"
  if curl -fsS --max-time 15 --resolve samaronefialho.dev:443:144.33.16.182 https://samaronefialho.dev/versionz >/dev/null 2>&1
  then echo "oci_resolve_probe=answered"; else echo "oci_resolve_probe=down"; fi
  gh run view "$CI_RUN" --json headSha,conclusion --jq '"ci_push_head_sha=\(.headSha)\nci_push_last=\(.conclusion)"'
  echo "ci_push_asserts=$(printf '%s\n' "$CI_LOG" | grep -oE 'assert [1-6]/6 ok' | sort -u | wc -l)/6"
  if printf '%s\n' "$CI_LOG" | grep -q 'PASS delete-and-confirm-gone' && ! printf '%s\n' "$CI_LOG" | grep -q 'FAIL step='
  then echo "ci_push_storage_probe=PASS"; else echo "ci_push_storage_probe=FAIL"; fi
  gh run list --workflow verify-live.yml --event schedule --limit 1 --json headSha,conclusion \
    --jq '.[0] | "verify_live_head_sha=\(.headSha)\nverify_live_last=\(.conclusion)"'
  if [ -z "$(git diff --stat "$WALK..$FINAL" -- . ':!docs' ':!ops/*.md' ':!tests' ':!ops/check_go_no_go.py' ':!.github')" ]
  then echo "walkthrough_code_diff=empty"; else echo "walkthrough_code_diff=changed"; fi
  if uv run pytest tests/ops/test_pilot_runbook_structure.py -q >/dev/null 2>&1
  then echo "runbook_structure=pass"; else echo "runbook_structure=fail"; fi
  echo "status_declares_v2=$(yes_no grep -qF '| `pilot-readiness-v2` | **current** |' docs/project-status.md)"
  echo "status_cutover_record=$(yes_no grep -q '^## Cutover record' docs/project-status.md)"
  echo "status_resend_superseded=$(yes_no grep -qF '| `resend-smtp-pilot-provider-amendment` | **superseded** |' docs/project-status.md)"
  echo "status_links_ledger=$(yes_no grep -qF 'ops/PILOT-GO-NO-GO.md' docs/project-status.md)"
  echo "restore_md_measured=$(yes_no grep -qF '(`rto_minutes=98`)' ops/RESTORE.md)"
} > "$OUT"
```

Then add one line by hand: `hc_verify_state=Up` or `hc_verify_state=Down`, read from
the `verify-live` check page in Healthchecks. Nothing else gets typed.

The walkthrough diff skips `tests`, `ops/check_go_no_go.py` and `.github` along with
the docs, because none of them is imported by the served process — `.github` is CI
plumbing, not served code. The pathspec in the plan text would count them, and a
test module already landed after the walkthrough, so that stricter form could never
go green without a re-run that proves nothing new.

## After exit 0

1. F1-F4 audit the capture and the evidence it points to.
2. The owner appends `pilot_start_authorized=<date>` to the capture. That's never a
   tracked commit, so `main` doesn't move after the capture.
3. The tag and the GitHub release follow `ops/RELEASES.md`, Phase B.
