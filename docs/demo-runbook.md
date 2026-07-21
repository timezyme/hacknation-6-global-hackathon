# Demo runbook — restarting and verifying the app

The deployed app is `trustdesk-spike` in the Free Edition workspace
(profile `trustdesk-spike`). Live URL:
`https://trustdesk-spike-7474651147859378.aws.databricksapps.com`

A cold start takes **2–3 minutes**. Warm the app at least 15 minutes before a
live demo. Reviews live in Lakebase and always survive restarts.

## Keep-alive during judging

Free Edition stops app compute at the workspace/account level roughly every
**6-8 hours** (compute message: "stopped due to workspace or account status").
This is not the 24-hour idle timer and no setting prevents it. A scheduled
workspace job (`keep-trustdesk-app-alive`, job id `544062991972699`) checks the
app **every 30 minutes** (July 19-24 2026 only) and restarts it only if it is
down, so worst-case downtime after a stop is ~30 min plus a ~3 min cold start.
The job never stops a healthy app. It runs the notebook
`/Workspace/Shared/keep-trustdesk-alive`, which also refuses to act after
2026-07-24, so the automation self-expires. Delete the job from Workflows after
judging.

There is no way to guarantee 100% uptime on Free Edition. Before a known
judging window, warm it by hand for certainty:

```sh
uv run python scripts/smoke_demo.py --profile trustdesk-spike
```

## Restart from the browser

1. Open the workspace: `https://dbc-0b2c41fb-f343.cloud.databricks.com`
2. Left sidebar → **Compute** → **Apps** tab → `trustdesk-spike`.
3. Click **Stop**, wait for the state to settle, then **Start**.
4. Wait until compute shows **ACTIVE**, then open the app URL and confirm the
   ranked list loads.

## Restart from the terminal

```sh
databricks apps stop  trustdesk-spike --profile trustdesk-spike
databricks apps start trustdesk-spike --profile trustdesk-spike
```

Or with the SDK (waits for each step):

```sh
uv run python -c "
from databricks.sdk import WorkspaceClient
w = WorkspaceClient(profile='trustdesk-spike')
w.apps.stop_and_wait('trustdesk-spike')
app = w.apps.start_and_wait('trustdesk-spike')
print(app.compute_status.state)
"
```

## Verify after any restart (or before a demo)

```sh
uv run python scripts/smoke_demo.py --profile trustdesk-spike
```

Expected: one JSON line ending in `"status": "pass"`, with `health_200`,
6 capabilities, a receipt showing attempt trail + referee + similar context,
and `review_round_trip: true`. The script also waits out a cold start (up to
7 minutes), so running it alone is enough to both warm and verify the app.

## If something is wrong

1. **App won't start / smoke fails on `options`:** check the app's service
   principal still has SELECT on `workspace.default.trustdesk_verdicts`,
   `trustdesk_receipts`, and `trustdesk_active_run`.
2. **Wrong or empty data:** the app reads the active batch run only. The
   previous completed run is retained; the active pointer lives in
   `workspace.default.trustdesk_active_run`.
3. **Live demo failure with judges waiting:** fall back to
   `artifacts/trustdesk-demo.mp4` (local copy) — it follows the same script.

## Redeploying (only if code ever changes — the demo is frozen)

Upload changed files to `/Workspace/Shared/trustdesk-spike`, then:

```sh
uv run python -c "
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import AppDeployment
w = WorkspaceClient(profile='trustdesk-spike')
d = w.apps.deploy_and_wait(app_name='trustdesk-spike',
    app_deployment=AppDeployment(source_code_path='/Workspace/Shared/trustdesk-spike'))
print(d.status.state)
"
```

Then rerun the smoke.
