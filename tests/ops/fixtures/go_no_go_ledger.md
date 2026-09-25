# Fixture go/no-go ledger

A minimal ledger with the real ledger's shape, for `ops/check_go_no_go.py` tests.

| line | kind | evidence_file | expected | rule |
| --- | --- | --- | --- | --- |
| S1 | final-release | `PILOT2-506-final.txt` | `final_main_sha=` `unpinned_versionz=` `lightsail_versionz=` `capture_utc=` `oci_resolve_probe=down` | same-day triple |
| S2 | final-release | `PILOT2-506-final.txt` | `ci_push_head_sha=` `ci_push_last=success` | last push CI green at the final SHA |
| | drill | `PILOT2-fixture-drill.txt` | `branch_protection=proven` | continuation row |
| S3 | final-release | `PILOT2-506-final.txt` | `verify_live_head_sha=` `verify_live_last=success` | last verify-live green at the final SHA |
| S4 | drill | `PILOT2-fixture-drill.txt` | `s4=PASS` | drill, historical |
| S5 | drill | `PILOT2-fixture-drill.txt` | `s5=PASS` | drill, historical |
| S6 | drill | `PILOT2-fixture-drill.txt` | `s6=PASS` | drill, historical |
| S7 | drill | `PILOT2-fixture-drill.txt` | `s7=PASS` | drill, historical |
| S8 | drill | `PILOT2-fixture-drill.txt` | `s8=PASS` | drill, historical |
| S9 | drill | `PILOT2-fixture-drill.txt` | `s9=PASS` | drill, historical |
| S10 | drill | `PILOT2-fixture-drill.txt` | `s10=PASS` | drill, historical |
| S11 | drill | `PILOT2-fixture-drill.txt` | `s11=PASS` | drill, historical |
| S12 | drill | `PILOT2-fixture-drill.txt` | `s12=PASS` | drill, historical |
| S13 | drill | `PILOT2-fixture-drill.txt` | `s13=PASS` | drill, historical |
| S14 | drill | `PILOT2-fixture-drill.txt` | `s14=PASS` | drill, historical |
| S15 | drill | `PILOT2-fixture-drill.txt` | `s15=PASS` | drill, historical |
| S16 | drill | `PILOT2-fixture-drill.txt` | `s16=PASS` | drill, historical |
| S17 | drill | `PILOT2-fixture-drill.txt` | `s17=PASS` | drill, historical |
| S18 | drill | `PILOT2-fixture-drill.txt` | `s18=PASS` `known_rows=t\|t` | walkthrough, historical |
| | final-release | `PILOT2-506-final.txt` | `walkthrough_code_diff=empty` | no served code changed since |
| S19 | drill | `PILOT2-fixture-drill.txt` | `s19=PASS` | drill, historical |
| S20 | final-release | `PILOT2-506-final.txt` | `status_links_ledger=yes` | truth documents at the final SHA |
| S21 | drill | `PILOT2-fixture-drill.txt` | `s21=PASS` | drill, historical |

Prose after the table is ignored.
