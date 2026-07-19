# Winning Demo Implementation Plan

## Goal

Ship a live Databricks Free Edition app that proves three things in one minute:

1. A planner can select a capability and region, inspect ranked facilities, read the exact evidence,
   and save an override with a note.
2. Every assessment is honest about what the dataset proves, what it does not prove, and which check
   made each decision.
3. A new check can be added or swapped through one implementation file and one configuration entry,
   without editing the pipeline.

The demo must use precomputed assessments. No model call or batch computation may run while the
planner waits.

## Why the plan is constructed this way

The phases create a working demo early, then deepen it in risk order.

1. **Lock one contract first.** `architecture.md` owns outcomes, receipts, ranking, and evaluation.
   The demo script owns the one-minute story. No five-document consistency exercise sits in the way.
2. **Prove the platform immediately.** A 60-minute spike deploys the app, binds Lakebase, writes one
   review, restarts, and reads it back. Deployment is too uncertain to leave until the end.
3. **Create a walking skeleton before evaluation work.** A bounded live-data slice flows through
   safe ingest and free checks into Delta, then into the deployed app and persistent override path.
4. **Prove modularity before adding expensive behavior.** Presence and vocabulary run through the
   extension seam before an LLM check is added.
5. **Timebox the free-check pilot.** A balanced labelled sample measures selective coverage without
   delaying the existence of the demo or looping until the numbers look good.
6. **Qualify one model, not two.** Start with Llama. Qwen is a configuration fallback only if Llama
   fails the written gate. Retrieval, a referee, MinerU, and LlamaIndex remain out of scope.
7. **Deepen the working path.** Atomic full-batch publication, API hardening, UI evidence, and final
   rehearsals improve a deployed demo that can remain the fallback at every step.

Every phase changes no more than five files. Human approval happens at four checkpoints: after
Phase 1, after Phase 3C, at the Phase 5B go/no-go, and before the Phase 9 freeze. Every other phase
begins as soon as the previous verification gate is green, and its report is reviewed
asynchronously. A later phase may not begin while the current phase's verification gate is red.

## Why executing this plan should produce a winning demo

The finished demo maps directly to the scoring rubric:

- **Evidence and Trust, 35%.** Exact row evidence, honest row-level source sets, explicit unknown and
  failure states, check identity, and a labelled evaluation report provide proof rather than claims.
- **Product Judgment, 30%.** The app implements the exact Facility Trust Desk workflow without a chat
  interface or unrelated features.
- **Technical Execution, 25%.** An early deployed Databricks App proves Unity Catalog reads and
  Lakebase persistence before the system is deepened with Delta publication, model serving, and
  MLflow traces. The live path performs reads and writes only.
- **Ambition, 10%.** The app does not merely claim modularity. A test and a real open-weight check prove
  that a new method can be introduced without changing the runner, while the evaluation report shows
  whether the method improved coverage and error rates.

The judge sees five pieces of proof: a ranked result, its receipt, an honest unknown, a persisted
override, and measured results for each check. Those five moments support the product's central claim.

## Scope decisions

### Required

- Parse and quarantine malformed records before claim generation.
- Resolve duplicate `unique_id` values into stable record keys.
- Generate claims only for the six target capabilities that a record asserts. Do not create the
  60,528-row cross product unless Phase 1 produces evidence that the product needs it.
- Run presence, vocabulary, and one open-weight entailment check through a configuration-driven
  pipeline.
- Treat a vocabulary non-match as abstention when semantic evidence could change the answer.
- Precompute verdicts, evidence receipts, and per-check evaluation metrics.
- Rank by record support, never by claimed facility quality.
- Persist confirm and override actions in Lakebase, while describing them as reviewer feedback rather
  than ground truth.
- Deploy and rehearse the live workflow on Databricks Free Edition.

### Outside the critical path

- Vector retrieval, MinerU, LlamaIndex, a second-model referee, and external source-page verification.
- Numeric facility confidence scores.
- Dynamic maps, Genie, Agent Bricks, and additional mission tracks.
- Live adjudication from the app.

These are excluded from the winning plan. Consider them only after the final submission candidate is
green and frozen.

## Evidence status before implementation

### Proven

- The live table contains 10,088 rows and 10,077 distinct `unique_id` values.
- Three visibly corrupted rows require quarantine.
- `capability`, `procedure`, and `equipment` contain JSON arrays that must be assessed item by item.
- Source URLs form a row-level source set, not a reliable sentence-to-URL mapping.
- Frontier models are unavailable in this workspace; Llama and Qwen are reachable.
- The current `ladder.py` hardcodes presence and vocabulary decisions and has no plugin seam.
- The live repository has 29 passing tests. The architecture's reference to 43 tests is stale.

### Needs proof

- The percentage of claims the free checks settle correctly.
- The prevalence of real, target-bound contradictions.
- Llama/Qwen accuracy on ambiguous evidence, throughput, and rate-limit behavior.
- Whether duplicate identifiers represent identical duplicate rows or different records.
- The exact live columns and values used for region filtering.
- Current test coverage. Pytest-cov is not installed until Phase 2A, so only the 29-test pass count is
  presently verified.

The needs-proof items become explicit gates below. They must not become implementation assumptions.

### Current platform primitives

- Databricks Apps forwards trusted user identity in `X-Forwarded-User`; do not accept reviewer identity
  from the request body. See [HTTP headers passed to Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/http-headers).
- Add Lakebase as an App resource so Databricks creates the app service-principal role and injects the
  connection settings. The demo uses request-scoped credentials and connections so no pool outlives
  the one-hour OAuth database credential. See
  [Lakebase App resources](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/lakebase) and
  [custom App connection](https://docs.databricks.com/aws/en/oltp/projects/tutorial-databricks-apps-autoscaling).
- Databricks starts the deployed process from the command in `app.yaml`. See
  [deploy a Databricks App](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/deploy).

## Amendments from the rubric re-review

Lines below marked *(amendment — not implemented)* were added after reviewing the judged rubric
against the built system. **None of them are built yet.** They exist so this work lands inside the
plan's gates instead of beside them. Already built and committed, but not yet visible in the app:
the vector index and its adapter (`src/trustdesk/similar.py`, `scripts/build_similar_index.py`);
their receipt wiring is an amendment like the rest.

**Vector index scope — subset, not the full 10k.** Free Edition embeds roughly 36 rows a minute,
so the full 9,997-row sync needs 3-4 hours and was abandoned inside the hackathon window. The live
index is built with `--limit 500`: the 500 richest-text rows whose text mentions one of the six
target capabilities, selected deterministically (see `LIMIT_FILTER_SQL` in the build script).
Similarity context in receipts therefore draws neighbors from that 500-row subset. Any claim of
"retrieval across 10k rows" in the demo or submission is wrong until the full index is rebuilt;
say "a capability-relevant subset" instead. Rebuilding the full index after the deadline is one
command: `scripts/build_similar_index.py` without `--limit`.

## Schedule guardrails and fallback modes

Timeboxes are ceilings. When a timebox expires, preserve the last green deployment and take the
listed fallback instead of expanding scope.

| Gate | Timebox | If it misses the gate |
|---|---:|---|
| Contract | 45 minutes | Resolve only a blocking ambiguity; defer prose cleanup. |
| Toolchain | 30 minutes | Keep the existing runtime and add only the checks needed by the next phase. |
| App + Lakebase spike | 60 minutes | Deployment is non-degradable: fix it before product depth. |
| Safe ingest | 60 minutes | Use a bounded validated slice; quarantine every unresolved record. |
| Check seam | 90 minutes | Ship only presence and vocabulary, but keep them behind the generic interface. |
| Walking skeleton | 120 minutes | Keep the smallest live slice that completes the required workflow. |
| Human-labelled pilot | 90 minutes | Publish the completed balanced sample and its actual denominator; label the report `in progress` if fewer than 60 claims are complete. |
| Model check | 60 minutes | Disable it and show unresolved cases; a free-check-only demo remains honest. |
| Referee pass *(amendment — not implemented)* | 60 minutes | Disable the referee in config; decisions display "not double-checked". |
| Full batch | 90 minutes | Keep the walking-skeleton Delta run active. |
| API and UI hardening | 120 minutes total | Revert the unfinished enhancement and keep the last green deployed workflow. |
| Final verification | 60 minutes | Freeze the last version that passed tests, restart smoke, and one-minute rehearsal. |

The timeboxes sum to 885 minutes, about 15 hours, before checkpoint waits and context switches.
Before starting Phase 1, compare that total against the actual time remaining. If it does not fit,
apply the cuts now rather than at expiry: run Phase 4 at the 60-claim floor, and give the model
check one attempt before taking its disable fallback instead of spending the timebox on its full
test matrix.

The 80% coverage gate remains. If a late change cannot meet it within its timebox, revert that change
instead of lowering the gate or risking the deployed demo.

At four hours remaining, accept no new features. At two hours remaining, freeze the last green
deployment and spend the remaining time only on restart checks, evidence review, and rehearsals.

## Phase 1 — Lock the product and evidence contracts

### Objective

Put the implementation contract in one technical source of truth and the judged workflow in one
script.

### Units of work

1. Update the architecture to: ingest and quarantine -> configured checks -> deterministic reduction
   and verdict -> atomic Delta publication -> read-only app.
2. Define three check outcomes: decision, abstention, and processing failure.
3. Define five user-visible verdict states: strong support, limited support, conflicting evidence,
   not enough evidence, and could not check.
4. Define the receipt fields: stable record key, facility ID, capability, field, item index, exact
   text, row source set, check ID and version, rationale, model and prompt version when applicable,
   pipeline run ID, and computation time.
5. Define the ranking rule and tie-breaks. Unresolved and uncheckable records remain visible but are
   not assigned a misleading low rank.
6. Define reviewer feedback as confirm or override plus a snapshot of the reviewed assessment. Do not
   call it measured accuracy.
7. Define the timeboxed labelled-pilot method and the one-minute demo script.

### Files, maximum four

- `docs/architecture.md`
- the demo script — new
- `docs/strategy.md` — superseded banner only
- `docs/verdict-contract.md` — superseded banner only

### Verification gate

- `docs/architecture.md` names the shipped pipeline, verdict states, receipt fields, model family,
  claim population, ranking rule, evaluation method, and fallback modes.
- `docs/strategy.md` and `docs/verdict-contract.md` receive short superseded banners that point to
  `docs/architecture.md`; their stale bodies are not rewritten.
- In `docs/architecture.md` and the demo script, Sonnet, Opus, live model calls, MinerU,
  LlamaIndex, retrieval, and referee behavior are absent or explicitly outside the critical path.
- The demo script completes the required workflow in 60 seconds or less when read aloud.
- No code changes occur in this phase.

### Verifiable outcome

The architecture and demo script form one approved contract and story. The two stale documents can
no longer be mistaken for current instructions. Stop for approval before Phase 2A.

## Phase 2A — Lock the verification and platform dependencies

### Objective

Make verification and the first deployment reproducible from one lockfile.

### Units of work

1. Add Ruff, mypy, pytest-cov, and strict project configuration.
2. Record the current baseline of 29 passing tests before changing behavior.
3. Add only the FastAPI, Databricks SDK, and PostgreSQL dependencies required by the platform spike.

### Files, maximum two

- `pyproject.toml`
- `uv.lock`

### Verification gate

- `uv sync --extra dev` completes from the lockfile.
- `uv run ruff check src tests` passes.
- `uv run mypy src` passes.
- `uv run pytest --cov=trustdesk --cov-fail-under=80 -q` passes.

### Verifiable outcome

The locked toolchain, baseline report, and platform imports are reproducible. Proceed to Phase 2B
when the gate is green.

## Phase 2B — Prove deployment and Lakebase in 60 minutes

### Objective

Remove the largest platform unknown before product work continues.

### Units of work

1. Create the smallest FastAPI app with health, write-probe, and read-probe endpoints.
2. Configure the real `app.yaml` command and add Lakebase as an App resource.
3. Use the app service principal and request-scoped database credentials; do not accept credentials or
   reviewer identity from request data.
4. Deploy to Databricks Free Edition, write one synthetic probe row with a parameterized query,
   restart the app, and read it back.
5. Record deployment, resource binding, service-principal access, restart persistence, and sanitized
   failure details in one machine-readable artifact.

### Files, maximum four

- `app.yaml` — new
- `app/main.py` — new
- `scripts/verify_platform_spike.py` — new
- `artifacts/platform-spike.json` — generated sanitized proof

### Verification gate

- The deployed health endpoint returns 200 after a cold start.
- The app creates, writes, and reads its Lakebase probe table through the bound resource.
- The same probe row remains readable after an app restart.
- The probe write is parameterized and public errors contain no SQL, stack trace, host, or credentials.
- `artifacts/platform-spike.json` records pass/fail, deployment time, restart result, and permission
  checks without hosts, credentials, reviewer identity, or raw data.
- No connection pool or cached credential can outlive the request that created it.

### Verifiable outcome

A live Databricks App and persisted Lakebase probe remove deployment from the list of late unknowns.
Proceed to Phase 3A when the gate is green.

## Phase 3A — Build safe ingest and stable record identity

### Objective

Turn live table rows into validated value objects without leaking raw rows or credentials into the
repository.

### Units of work

1. Parse `description` as text; parse `capability`, `procedure`, and `equipment` item by item; and
   parse `source_urls` as the row-level source set.
2. Validate facility name, identifier, and array fields.
3. Quarantine malformed rows as processing failures rather than missing evidence.
4. Inspect duplicate-ID groups. Deduplicate byte-identical records or add a deterministic row hash to
   distinguish different records. Record the chosen rule in the value-object contract.
5. Identify and validate the exact columns and values used for region filtering.
6. Generate claims only when the capability array asserts one of the six target capabilities.
7. Add a safe audit entry point to `ingest.py` that writes aggregate counts only.

### Files, maximum five

- `src/trustdesk/models.py` — new
- `src/trustdesk/ingest.py` — new
- `tests/test_ingest.py` — new, using synthetic fixtures only
- `artifacts/ingest-audit.json` — generated aggregate proof, no raw rows

### Verification gate

- `uv run ruff check src tests` passes.
- `uv run mypy src` passes.
- `uv run pytest tests/test_ingest.py -q` passes.
- Replaying the same synthetic records produces identical record keys and claims.
- The live aggregate audit reports 10,088 input rows, unique generated record keys, the quarantine
  count, the asserted-claim count, and region-field coverage without including facility text, URLs,
  hosts, IDs, or secrets.
- If the quarantine count differs from the authoritative audit's three corrupted rows, stop and
  reconcile the parser with live evidence before continuing.

### Verifiable outcome

`artifacts/ingest-audit.json` and passing ingest tests prove the source population, identity rule,
claim count, region contract, and quarantine behavior. Proceed to Phase 3B when the gate is green.

## Phase 3B — Prove the check extension seam

### Objective

Make “one implementation file plus one configuration entry” true before adding an LLM.

### Units of work

1. Define one check interface: one claim plus its complete parsed evidence bundle in; zero or more
   item findings out. Every evidence coordinate is a decision, abstention, or processing failure,
   and every decision cites its field and item index.
2. Include stable `check_id`, implementation version, rationale, evidence coordinates, and cost tier in
   every outcome. Do not use a numeric rung as identity.
3. Refactor presence and vocabulary into independent flat modules.
4. Make the pipeline load and order checks from configuration. Invoke each check once per claim with
   the currently unresolved evidence bundle; the first accepted decision for each evidence
   coordinate wins.
5. Make vocabulary non-matches abstain when semantic interpretation could overturn them.
6. Preserve the full attempt history for receipts and evaluation.
7. Add a test-only check through one new implementation and one temporary config entry. The test must
   pass without changing pipeline code.

### Files, maximum five

- `src/trustdesk/ladder.py`
- `src/trustdesk/check_presence.py` — new
- `src/trustdesk/check_vocabulary.py` — new
- `config/checks.toml` — new
- `tests/test_ladder.py`

### Verification gate

- Existing behavior tests remain green except where the approved contract deliberately changes a
  vocabulary non-match from decision to abstention.
- Tests cover ordering, abstention, processing failure, unknown check configuration, stable check
  identity, and attempt history.
- A test adds and removes a check without editing `ladder.py`, marks, reducers, or UI code.
- `uv run ruff check src tests`, `uv run mypy src`, and `uv run pytest -q` pass.

### Verifiable outcome

The repository contains a working configured pipeline and a regression test that proves the extension
claim. Proceed to Phase 3C when the gate is green.

## Phase 3C — Deploy the walking skeleton

### Objective

Create the first submit-ready demo before human labelling or model work begins.

### Units of work

1. Select a reproducible live-data slice with at least five valid candidate facilities per capability
   across at least two regions.
2. Run safe ingest plus the configured free checks and publish one completed, versioned Delta slice.
3. Extend the deployed app to filter by capability and region, rank by deterministic record support,
   and open an exact item-level receipt with the row-level source set.
4. Persist one confirm or override action with a note in Lakebase.
5. Deploy, restart, and run the required one-minute path without a model call.

### Files, maximum five

- `src/trustdesk/skeleton_batch.py` — new
- `app/main.py`
- `app/index.html` — renamed from `app/mock.html`, preserving the settled design
- `tests/e2e/test_skeleton.py` — new
- `artifacts/walking-skeleton-proof.json` — generated sanitized proof

### Verification gate

- The active Delta slice is marked complete and contains all six capabilities and at least two regions.
- The deployed app completes capability -> region -> ranking -> receipt -> override.
- The receipt shows the exact field item, row source set, deciding check, and what remains unknown.
- The override remains after restart, and the app makes no model request.
- The end-to-end test, full existing suite, and 80% coverage gate pass.

### Verifiable outcome

`artifacts/walking-skeleton-proof.json` identifies the active slice and records the deployed smoke and
restart results. If every later enhancement fails, this remains the honest fallback demo. Stop for
approval before Phase 4.

## Phase 4 — Run the timeboxed free-check evaluation gate

### Objective

Produce a preliminary, timeboxed measure of whether deterministic checks are safe and how much work
remains for the model check.

### Units of work

1. Build a reproducible queue of 120 candidate claims, exactly 20 per capability and ordered in
   balanced six-claim waves. Interleave development and holdout claims across waves so that stopping
   after any completed wave leaves every capability with labelled claims in both splits, near the
   60/40 ratio. The audit's stronger 300-claim experiment is deferred until after a submission
   candidate is frozen.
2. Store labels in a Databricks table, not the repository. Label evidence as support, refutation,
   irrelevant, or uncertain without showing the system decision first.
3. Freeze a 60/40 development and holdout assignment before tuning: 12 development and eight holdout
   claims per capability at the 120-claim target. Record a hash of both manifests.
4. Tune vocabulary only against the development split. Run the accepted free checks against the
   holdout once, after the rules are frozen.
5. Stop labelling at 90 minutes after finishing the current balanced wave. Sixty claims is the
   minimum complete pilot; below that, publish the actual denominator and label the report
   `in progress — insufficient sample`.
6. Report selective coverage, abstention rate, decision precision, and errors by capability and
   check on both splits. Never combine them into one headline number.
7. Report target-bound contradiction prevalence separately from generic negative-language hits and
   estimate the number of claims that would require a model call.

### Files, maximum five

- `src/trustdesk/evaluation.py` — new
- `scripts/run_pilot.py` — new
- `tests/test_evaluation.py` — new
- `artifacts/pilot-summary.json` — generated aggregate results
- `docs/pilot-results.md` — generated explanation and go/no-go decision

### Verification gate

- The label table contains up to 120 distinct claims in balanced capability counts. The report states
  the actual development and holdout denominators.
- Evaluation code rejects duplicate labels, missing labels, leaked system predictions, and wrong class
  names.
- No accepted free rule has an observed false-support or false-conflict example on the sealed
  holdout. A rule that fails this gate must abstain on that case class and the phase continues; the
  team does not retune against holdout. Report confidence intervals so zero observed errors is not
  presented as certainty.
- `artifacts/pilot-summary.json` records coverage and errors per capability and check, not one blended
  number, plus the split hashes and rule-configuration hash.
- `docs/pilot-results.md` states the actual label count, status, projected model-call count, and
  whether a model check is economically plausible.
- Neither artifact describes the 120-claim result as final accuracy or as equivalent to the audit's
  proposed 300-claim experiment.
- `uv run ruff check src tests scripts`, `uv run mypy src scripts`, and `uv run pytest -q` pass.

### Verifiable outcome

The committed report proves what the completed sample says. Unsafe free rules have been forced to
abstain, so the walking demo remains usable even when coverage is low. Proceed to Phase 5A when the
gate is green.

## Phase 5A — Lock the model and tracing dependencies

### Objective

Make the production model path reproducible before changing the check pipeline.

### Units of work

1. Add only the official Databricks and MLflow dependencies needed by the chosen adapter and tracing
   path.
2. Lock their transitive dependencies and verify a clean install.

### Files, maximum two

- `pyproject.toml`
- `uv.lock`

### Verification gate

- `uv sync --extra dev` completes from scratch.
- A no-data import smoke test for the selected SDK and MLflow passes.
- The full existing verification suite remains green.

### Verifiable outcome

The locked environment can load the production model and tracing primitives. Proceed to Phase 5B
when the gate is green.

## Phase 5B — Add and qualify one Llama entailment check

### Objective

Prove that the same pipeline can host a real remote check and that the remaining batch is feasible.

### Units of work

1. Add one entailment module containing a small model-client interface, a Databricks Foundation Model
   adapter, and the check implementation. Tests provide an in-memory adapter.
2. Send one evidence bundle per claim, not one model call per evidence item.
3. Return structured support, conflict, irrelevant, or uncertain outcomes with exact field and item
   coordinates plus a short rationale.
4. Configure Llama first. Run Qwen on the development split only if Llama fails structured-output,
   error, or throughput gates. Do not run a comparative bakeoff or add a frontier-model fallback.
5. Classify timeout, rate limit, invalid output, and exhausted retry as processing outcomes. Never
   convert them to missing or silent evidence.
6. Trace model calls in MLflow and record endpoint, model, prompt, parser, latency, and retry count.
7. Evaluate the selected configuration once on holdout abstentions. If it produces an observed false
   support or false conflict there, disable the model check rather than selecting again on holdout.

### Files, maximum five

- `src/trustdesk/llm_check.py` — new
- `tests/test_llm_check.py` — new
- `scripts/run_model_pilot.py` — new
- `artifacts/model-pilot-summary.json` — generated aggregate proof
- `config/checks.toml`

### Verification gate

- Behavior tests cover support, conflict, irrelevant, uncertain, malformed model output, one transient
  retry followed by success, exhausted retries, and duplicate invocation.
- The production adapter and in-memory adapter pass the same contract tests.
- On development abstentions, a candidate fails qualification if it produces an observed false
  support or false conflict, cannot return contract-valid output after one retry, or cannot complete
  the projected batch with 30% measured rate-limit headroom.
- A diff against the approved Phase 3B checkpoint shows no change to `src/trustdesk/ladder.py`; this
  is the real modularity proof.
- A live MLflow run contains the pilot smoke traces and aggregate metrics without secrets or raw
  credentials.
- `artifacts/model-pilot-summary.json` records the attempted model, why a Qwen fallback was or was not
  needed, the selected configuration, holdout results, call rate, p95 latency, quota failures, and
  projected full-batch cost.
- Projected full-batch model work can complete twice within the remaining pre-demo window with at
  least 30% measured rate-limit headroom. If it cannot, cap model adjudication and persist the rest as
  unresolved rather than adding more infrastructure.
- `uv run ruff check src tests`, `uv run mypy src`, and `uv run pytest -q` pass.

### Verifiable outcome

The config contains one qualified remote check or explicitly disables it. The runner remains
unchanged, the labelled artifact measures behavior, and MLflow proves traceability and runtime. At
this go/no-go checkpoint, approve full, capped-model, or free-check-only mode before Phase 6.

## Phase 5C — Referee pass over free-check decisions *(amendment — implemented)*

**Status:** the module, tests, config, contract update, and `artifacts/referee-summary.json` are
done and committed. Phase 6 completed the batch-receipt wiring; displaying referee findings remains
part of Phase 8.

### Objective

Make the app double-check its own work: every decision produced by a free check is independently
re-examined, and disagreement is shown, never hidden. This is the brief's named validator stretch
goal, inside the shipped app.

### Units of work

1. Add one referee module that re-evaluates each decided evidence coordinate with a method other
   than the one that decided it: the capped Llama bundle when enabled, otherwise a rule-based
   internal-consistency validator.
2. Record agree, disagree, or could-not-referee per decision, with rationale, as receipt data. The
   referee never changes the verdict; disagreement displays as "checks disagree on this decision."
3. Cap referee model calls in config. The decided subset is far smaller than the full claim
   population, so the cap must fit inside the rate-limit headroom measured in Phase 5B.
4. Extend the receipt contract in `docs/architecture.md` with the referee fields.
5. Publish referee agreement counts per check for the Phase 8 method panel.

### Files, maximum five

- `src/trustdesk/referee.py` — new
- `tests/test_referee.py` — new
- `config/checks.toml`
- `docs/architecture.md`
- `artifacts/referee-summary.json` — generated aggregate proof

### Verification gate

- Tests cover agreement, disagreement, referee processing failure, the config cap, and the
  disabled fallback.
- A diff shows no change to `src/trustdesk/ladder.py`.
- Referee outcomes appear in receipt data without altering any verdict.
- With the referee disabled in config, pipeline output is identical to the Phase 5B baseline.
- `uv run ruff check src tests`, `uv run mypy src`, and `uv run pytest -q` pass.

### Verifiable outcome

Every displayed decision carries a second opinion or an honest "not double-checked" label.

## Phase 6 — Precompute complete, reproducible result sets

### Objective

Produce the exact data the app will read and prevent partial batches from reaching the demo.

### Units of work

1. Reduce item findings to field marks with explicit mixed-support, mixed-conflict, uncertainty, and
   failure rules.
2. Derive verdicts deterministically from the field marks.
3. Write verdicts, evidence receipts, quarantined records, and batch manifests to Delta tables.
4. Make rerunning the same pipeline version idempotent.
5. Record expected count, actual count, check configuration hash, model and prompt version, input table
   version, timestamps, and completion status in the manifest.
6. Publish a run to the app only after counts and checks pass. Preserve the previous successful run.
7. *(amendment — implemented)* Fetch similar-facility context from the vector index at publish
   time, through `src/trustdesk/similar.py`, and store it with each receipt. Batch-time only,
   ranked claims only; a fetch failure degrades to "context unavailable", never blocks
   publication. Wired via a `SimilarCallback` mirroring the referee callback; enabling it changes
   the run id through the config hash.

### Files, maximum five

- `src/trustdesk/batch.py` — new
- `src/trustdesk/sink.py` — new
- `src/trustdesk/marks.py`
- `tests/test_batch.py` — new
- `tests/test_marks.py`

### Verification gate

- Tests cover mixed evidence, all-abstain, processing failure, quarantine, duplicate record delivery,
  partial write, rerun, and failed-run non-publication.
- The full live run produces one completed manifest whose expected and actual counts agree.
- Every verdict resolves to its evidence items and full check attempt history.
- The active-result pointer references only the completed run, and the previous completed run remains
  queryable.
- A bounded SQL smoke query confirms all six capabilities and all verdict states expected from the
  pilot without printing raw evidence.
- *(amendment — not implemented)* The demo script's chosen examples span the verdict states
  actually present in the full run, including at least one non-ranked state (not enough evidence,
  could not check, or does not claim). A single-verdict demo slice fails this gate.
- `uv run ruff check src tests`, `uv run mypy src`, and `uv run pytest -q` pass.

### Verifiable outcome

The Delta tables and completed batch manifest form a reproducible, rollback-safe demo dataset.
Proceed to Phase 7 when the gate is green.

## Phase 7 — Harden the read API and persistent review workflow

**Status: implemented and reviewed; deployed verification pending.** The repository adapter in
`app/repositories.py` translates the Phase 6 receipt shape to the UI contract (the carried
blocker), filters quarantine receipts out of the evidence join, fails loudly on truncated result
sets, and migrates the review schema on both read and write paths. The app defaults to the proven
walking-skeleton table; set `TRUSTDESK_RESULTS_SOURCE=batch` (plus the warehouse id) to serve the
active batch run — flip it during Phase 9 deployment. Still open from the Phase 6 review: the
manifest hardcodes `model_mode="disabled"` (guard before any config enables a metered check), and
the production `DatabricksSink` plus this phase's live restart/persistence check await the first
full live run in Phase 9.

### Objective

Deepen the walking skeleton without changing its deployed workflow or adjudicating in the request
path.

### Units of work

1. Add read endpoints for capability and region results, facility detail, receipts, and aggregate
   method metrics.
2. Apply the approved deterministic ranking and return its explanation.
3. Harden confirm and override writes to snapshot the assessment, run ID, reviewer, note, and
   timestamp.
4. Add repository interfaces with in-memory test adapters and Databricks/Lakebase production adapters.
   The Lakebase adapter uses request-scoped credentials and connections through the App resource.
5. Validate capability, region, record key, and note length. Use parameterized queries and derive the
   reviewer from trusted `X-Forwarded-User` rather than request data.
6. Validate the request `Origin` against the trusted forwarded host for review writes, return generic
   public errors, and redact logs.
7. Keep review-write failure separate from verdict-read availability.

### Files, maximum five

- `app/main.py`
- `app/repositories.py` — new
- `tests/test_app.py` — new
- `pyproject.toml`
- `uv.lock`

### Verification gate

- Tests cover filtering, stable ordering, ties, unresolved separation, receipt lookup, invalid input,
  SQL-injection input, duplicate confirm, override replacement, review snapshotting, identity
  spoofing, and Lakebase failure.
- No request handler imports or invokes the check pipeline or model client.
- The in-memory and production repositories pass the same behavior contract.
- Database queries are parameterized, secrets come only from environment bindings, and public errors
  contain no stack traces, SQL, credentials, hosts, or internal identifiers.
- A local restart retains review data when the Lakebase adapter is used.
- `uv run ruff check src app tests`, `uv run mypy src app`, and `uv run pytest -q` pass.

### Verifiable outcome

The API can complete the whole planner workflow against precomputed data, and review state survives a
restart. Proceed to Phase 8 when the gate is green.

## Phase 8 — Harden the walking-skeleton interface

**Status: implemented.** All five verdict states render; unranked
states appear in labelled sections outside the ranking; referee second opinions and attempt trails
show per decision; the measurements panel serves the pilot tables (with confidence intervals) and
referee totals from `/api/methods`; CSP and security headers are enforced server-side; the
one-minute workflow has an e2e test covering unresolved and processing-failure records. The first
full live batch is published (10,505 verdicts: 1,174 strong support, 9,331 not enough data — no
conflicting or could-not-check rows exist in this corpus) and the batch read path is verified
against it. Similar-facility context is wired batch-side and rendered in the receipt panel.

### Objective

Keep the approved visual design while deepening evidence and evaluation displays.

### Units of work

1. Preserve the settled design and keep the hackathon UI in one dependency-free HTML file.
2. Remove any hardcoded catalog or in-memory review fallback left by the walking skeleton.
3. Show rank meaning, verdict, exact evidence item, field, row source set, deciding check and version,
   rationale, model-call status, and what remains unknown.
4. Show unresolved and uncheckable records separately from ranked records.
5. Show the pilot's per-check coverage and error summary without calling reviewer feedback accuracy.
6. Render planner-entered notes as text, enforce a same-origin content security policy, and send only
   same-origin review requests.
7. Add an end-to-end test for the exact one-minute workflow.
8. *(amendment — partially implemented)* Show, per decision: the attempt trail (which checks
   abstained before one decided) — **done, shipped in `app/index.html` ahead of this phase** — plus
   the referee outcome or "not double-checked" and the similar-facility comparison context with its
   honest framing (both not implemented). Show the pilot's confidence intervals on the per-check
   method panel (not implemented).

### Files, maximum five

- `app/index.html`
- `tests/e2e/test_demo.py` — new
- `pyproject.toml`
- `uv.lock`

### Verification gate

- The end-to-end test selects a capability and region, verifies stable ranking, expands a receipt,
  saves an override, reloads, and verifies the persisted note.
- The test also exercises an unresolved record and a processing-failure record.
- Browser inspection shows no console errors, failed requests, hidden raw exceptions, or HTML injection.
- Response headers enforce the approved content security policy, and the browser stores no credentials
  or bearer tokens.
- The page makes no model request and contains no hardcoded facility catalog.
- `uv run ruff check src app tests`, `uv run mypy src app`, and the full test suite with at least 80%
  coverage pass.

### Verifiable outcome

The local app completes the exact judged workflow through real interfaces. Proceed to Phase 9 when
the gate is green.

## Phase 9 — Deploy, restart, rehearse, and freeze the demo

**Status: FROZEN — approved 2026-07-19.** Code and data on the demo path are locked; only checks and rehearsals from here. Staged deploy done
(new code on legacy source, then `TRUSTDESK_RESULTS_SOURCE=batch`); `scripts/smoke_demo.py`
passes before and after a cold restart; a pre-restart review persisted with its snapshot; the
app service principal was granted SELECT on the three batch tables. Evidence:
`artifacts/demo-proof.md`. Open: freeze approval, three timed rehearsals, and the
`docs/submission.md` bonus paragraph.

### Objective

Prove the same workflow works on Databricks Free Edition and preserve evidence that it worked.

### Units of work

1. Redeploy the already-proven Databricks App against the active completed batch and Lakebase.
   Recheck the current `app.yaml` and App resource bindings against official documentation.
2. Add a read-only smoke script for app health, result filtering, receipt detail, and review persistence.
3. Restart the app, rerun the smoke, and confirm the saved review remains.
4. Run the full compiler, linter, tests, coverage, and end-to-end suite.
5. Rehearse the one-minute script three times against the deployed app.
6. After the final green rehearsal, obtain freeze approval, then freeze code and data. Keep the
   previous successful batch available.
7. *(amendment — demo script done; submission pending)* Update `docs/submission.md` and the demo
   script to call out the brief's research questions this system answers: confidence intervals
   around check performance, corroborated claims versus bare listings via the four field grades,
   and the data-desert versus medical-desert distinction. The brief awards a real-impact bonus only
   if this is said out loud. The demo-script callouts are done (bolded);
   `docs/submission.md` still needs its paragraph.

### Files, maximum five

- `app.yaml`
- `scripts/smoke_demo.py` — new
- `docs/demo-runbook.md` — new
- `docs/submission.md`
- `artifacts/demo-proof.md` — generated, sanitized evidence only

### Verification gate

- The deployed app starts from a cold restart and serves the expected active run.
- The smoke script passes before and after restart.
- A review written before restart is present afterward.
- `uv run ruff check src app tests scripts` passes.
- `uv run mypy src app scripts` passes.
- `uv run pytest --cov=trustdesk --cov=app --cov-fail-under=80` passes.
- The end-to-end test passes against the deployed URL.
- Three timed rehearsals finish within 60 seconds without model calls, manual data repair, or terminal
  intervention.
- `artifacts/demo-proof.md` records pass/fail, test counts, coverage, active batch ID, deployment time,
  restart verification, and rehearsal durations. It contains no credentials, hosts, raw rows, or
  reviewer identities.
- A final secret scan finds no tokens, passwords, connection strings, raw identities, or leaked
  Databricks headers in tracked files or git diff.

### Verifiable outcome

The repository contains a reproducible runbook and sanitized proof, while the live Databricks App
demonstrates the required workflow from a cold start.

## Final acceptance checklist

- [ ] The app is live on Databricks Free Edition.
- [ ] Capability and region filtering use the live dataset.
- [ ] Ranking is deterministic and explained as record support, not facility quality.
- [ ] Every displayed assessment links to exact row evidence and the row-level source set.
- [ ] Unresolved, missing, conflicting, and processing-failure states remain distinct.
- [ ] Presence, vocabulary, and one Llama/Qwen check run through the same configured pipeline.
- [ ] A regression test proves a check can be added without editing the runner.
- [ ] The timeboxed pilot states its denominator and shows coverage and errors per capability and check.
- [ ] Reviewer feedback is persisted but is not described as ground-truth accuracy.
- [ ] The app performs no live adjudication.
- [ ] Only completed result sets can become active.
- [ ] Overrides survive an app restart.
- [ ] Ruff, mypy, tests, coverage, local end-to-end, deployed smoke, and restart checks pass.
- [ ] The one-minute demo completes three consecutive times.
- [ ] *(amendment — not implemented)* Every free-check decision shows a referee outcome or an
  honest "not double-checked" label.
- [ ] *(amendment — implemented; verify on the live run)* Receipts show the attempt trail and
  similar-facility context framed as comparison, never verification.
- [ ] *(amendment — not implemented)* The demo examples include at least one non-ranked honesty
  state.
- [ ] *(amendment — not implemented)* The submission and demo name the answered research questions
  for the real-impact bonus.

## Execution rule

Implement Phase 1 only after this plan is approved. At the end of each phase, provide the changed
files, commands run, artifacts or external state produced, and the exact pass/fail result. Wait for
explicit approval only at the four checkpoints: after Phase 1, after Phase 3C, at the Phase 5B
go/no-go, and before the Phase 9 freeze. Every other phase begins as soon as its predecessor's gate
is green.
