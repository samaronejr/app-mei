# Pilot monitor-to-action table — stub

> **PILOT-207 stub only.** PILOT-502 completes this runbook after the operator
> supplies the named contacts; no row below is an incident procedure yet.

| Monitor | Signal | Action placeholder for PILOT-502 |
| --- | --- | --- |
| UptimeRobot M1 — public `/healthz` | Any required JSON field differs from `status=ok`, `scheduler=alive`, `backup=fresh`, `disk=ok`. | Name the owner, triage path, escalation, and stop-condition link. |
| UptimeRobot M2 — public `/readyz` | Any required JSON field differs from `status=ready`, `database=ok`, `redis=ok`. | Name the owner, dependency triage path, escalation, and stop-condition link. |
| UptimeRobot M3 — public `/versionz` | `release` is not a lowercase 40-hex SHA. | Name the owner, release-drift triage path, and escalation. |
| UptimeRobot M4 — pilot portal `/healthz` | Any required JSON field differs from the M1 contract. | Name the owner, portal-host triage path, escalation, and stop-condition link. |
| Scheduled `verify-live` workflow / Healthchecks | JSON contract failure, live release differs from `main`, TLS has fewer than 14 days remaining, or the daily ping is absent. | Name the owner, verification path, escalation, and TLS-renewal path. |
| Sentry — Celery worker/beat | An unhandled task exception reaches Sentry. | Name the owner, task triage path, retry decision, and escalation. |
