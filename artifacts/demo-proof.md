# Demo proof — deployed batch-backed Trust Desk

Generated 2026-07-19 after the staged Phase 9 deployment. Sanitized: no credentials,
hosts, raw rows, or reviewer identities.

## Active data

- Active batch run: `6ac841c3c3b7ba52` (prefix) — published atomically, previous run retained.
- Counts reconciled: 10,077 facilities · 10,505 verdicts · 10,508 receipts · 0 orphans.
- Verdict distribution: 1,174 strong support · 9,331 not enough data. No conflicting or
  could-not-check rows exist in this corpus; the demo shows the states the data contains.
- Similar-facility context present on ranked receipts (capability-relevant 500-row index),
  framed as comparison, not verification.
- Walking-skeleton table preserved (60 rows) as rollback.

## Deployment sequence

1. Stage 1 — new app code, legacy data source: deployment SUCCEEDED, full smoke pass.
2. Stage 2 — `TRUSTDESK_RESULTS_SOURCE=batch`: deployment SUCCEEDED. First smoke failed
   (`options`) because the app service principal lacked SELECT on the three batch tables;
   grants added; smoke pass.
3. Cold restart (stop/start): app ACTIVE, health 200.

## Smoke results (after restart)

- health 200 · 6 capabilities · model_requests 0
- results: 7 ranked + 60 unranked in the probe region, ranking rule stated
- receipt: 95 items, 9 decided, attempt trail present, referee opinion present,
  similar context present
- methods: pilot metrics (with confidence intervals) and referee summary served
- review round trip: write 201, read-back correct
- Review written before the restart persisted after it, with its snapshot
  (verdict `strong_support`, deciding checks `vocabulary`).

## Local verification at this commit

- 163 tests passed · coverage 81.10% (gate 80) · ruff clean · mypy clean (22 files)
- Secret scan of tracked files: clean (one false positive: the psycopg `password=`
  parameter fed by the request-scoped OAuth credential).

## Still open

- Freeze approval and three timed one-minute rehearsals (human steps).
- `docs/submission.md` real-impact bonus paragraph.
