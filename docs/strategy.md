# Strategy — Facility Trust Desk

High-level plan for Challenge 04. What we are building, why it wins, and how we proceed.
Grounded against the live workspace and current Databricks docs on 2026-07-18, not from memory.

## The thesis

> Most entries will rank facilities. We will **adjudicate claims, show the receipt for every
> verdict, and publish how often we are wrong.**

Three sentences, three rubric buckets. The brief says outright: "Since there is no ground truth, we
value apps that double-check their own work." Almost nobody will take that literally, because doing
it means admitting an error rate. That is the opening.

## What actually wins here

The rubric is 35% Evidence and Trust, 30% Product Judgment, 25% Technical Execution, 10% Ambition.
The 35% is the largest single bucket and the hardest to fake. Our four bets, in priority order:

### Bet 1 — The adjudication ladder (aims at 35%)

A claim is not scored by one model call. It descends a ladder, and **the first rung that can decide,
decides**. Every verdict records which rung fired and why.

| Rung | What it does | Cost |
|---|---|---|
| 0. Presence | Is the field empty or populated? Separates "no data" from "has data". | Free |
| 1. Vocabulary | Target-specific medical terms plus bidirectional containment and agreement. | Free |
| 2. Retrieval | Vector Search for supporting **and refuting** passages. | Cheap |
| 3. Entailment | LLM adjudication (Sonnet 5), only where rungs 0-2 are inconclusive. | Metered |
| 4. Referee | Opus 4.8, only when an independent second check disagrees with rung 3. | Rare |

Why this is the right shape, not just a cost optimization:

- **Deterministic verdicts are directly explainable.** A planner can be shown exactly why a
  facility was marked "no data at all" — the field is empty. No trust required.
- **The model is the last resort, not the first.** That is the opposite of a chatbot behind a
  search box, which the brief explicitly warns against under Product Judgment.
- **It is designed to survive rate limits.** 10,088 facilities times six capabilities is up to
  60,528 adjudications. Naively making one LLM call per case is not survivable. The labeled pilot
  must prove how many cases the cheap rungs settle correctly; we no longer assume a majority.

### Bet 2 — We retrieve refutation, not just support (aims at 35%)

Standard RAG retrieves evidence that supports a query. The interesting failures in this dataset are
facilities whose own description contradicts their capability list — "all critical and
ventilator-dependent cases are referred to Patna" against a record claiming ICU.

So we run retrieval twice per claim: once for corroboration, once for **refutation patterns**
(referral-out language, closure, "not available on site", "no facility maintained"). Contradiction
is a first-class retrieval target, not something we hope the model notices.

This is the single hardest technical problem in the build and the one that produces the most
striking demo moment. Budget it as core work.

### Bet 3 — The review workflow IS the calibration loop (aims at 35% and 10%)

This is the idea we think nobody else will have.

The brief requires persisting reviewer actions — notes, overrides, review decisions. Everyone will
treat that as a storage requirement. We treat it as **a labeling pipeline**.

Every time a planner confirms or overrides an assessment, that decision lands in Lakebase as a
labeled example. Once we have a few hundred, the app can report its own measured precision per
verdict state, **with Wilson score intervals** so small samples read as uncertain rather than
authoritative.

The app then shows something no ranking dashboard shows:

> Strong record support: 87% correct (n=64, 95% CI 76-93%)
> Conflicting evidence: 71% correct (n=21, 95% CI 50-86%) — small sample, treat with care

This answers the brief's own stated open research question verbatim: *"How do you quantify trust
when there is no ground truth? Can statistics-based methods create prediction intervals around your
conclusions so planners know what is solid vs speculative?"* The organizers said they do not have
this answer. Answering it is the Real-Impact Bonus (stretch goal 4) and it is what "double-check
your own work" actually means.

It also closes the exact hole we found in TimeZyme: confidence signals with no eval harness to
calibrate them. We are not repeating that.

### Bet 4 — Receipts at every step via MLflow 3 (aims at 25% and 10%)

Stretch goal 1 asks for "the exact sentence and the reasoning step that produced each trust signal
 — extraction, scoring, ranking, with receipts at every step," and names MLflow 3 Tracing as the
hint. Because the ladder is already a sequence of discrete, logged rungs, tracing it is close to
free. Each verdict in the UI carries its trace, so "why did you say that?" is answerable down to the
rung and the sentence.

## What we deliberately are not doing

Scope discipline is how this ships in a hackathon.

- **Not building all four tracks.** The brief says do not. We build Facility Trust Desk.
- **Not building a chat interface.** The brief warns against "technology behind a chat box" in the
  30% bucket. The planner picks a capability and a region. That is the whole input surface.
- **Not inventing a numeric confidence score.** Decided already and it still holds: an uncalibrated
  "0.78" is invented precision. We publish measured precision instead, which is a different and
  defensible thing.
- **Not using Agent Bricks.** It is unavailable on Free Edition (see constraints below). The brief
  names it, but the brief describes an aspirational stack.
- **Not putting an external LLM in the primary path.** An `OPENAI_API_KEY` is available and works,
  but it stays a fallback. Two concrete reasons beyond preference: it cannot back the AI Search
  index (Free Edition allows Delta Sync only, which requires a Databricks embedding endpoint), and
  it does not buy referee independence, because Databricks already serves five model families
  in-workspace — Anthropic, Google, OpenAI, Alibaba, Meta. Its real uses are quota relief if Free
  Edition rate limits bite, local prompt iteration, and the `-pro` reasoning tiers if a class of
  hard contradictions defeats the in-workspace models.

## Free Edition constraints — verified, and they shape the design

Verified against the live workspace (REST probes returned 200 for vector-search, database instances,
apps, genie, unity catalog) and against `docs.databricks.com/aws/en/getting-started/free-edition-limitations`.

| Capability | Free Edition reality | Design consequence |
|---|---|---|
| Databricks Apps | Available. **3 apps per account**, auto-stops 24h after start/redeploy, restartable | Restart before demo. Never rely on in-app memory; the container is wiped on restart |
| AI Search (Vector Search) | Available. **1 endpoint, 1 search unit, Delta Sync only** — Direct Vector Access unsupported | One index, sourced from a Delta table. 10k rows is trivial against a 2M-vector unit |
| Lakebase | Available. **1 project**, scale-to-zero Postgres | This is our persistence for review decisions. App gets `PGHOST`/`PGUSER` etc. injected automatically |
| Model Serving | **Frontier models are rate-limited to 0.** Claude Opus 4.8, Claude Sonnet 5, Gemini 3.5 Flash all return 403 despite listing as READY. Open-weight models (Llama 3.3 70B, Qwen3-Next 80B, Gemma 3) work at sub-second latency. All 3 embedding models work | Rung 3 uses open-weight in-workspace models. Rung 4 referee uses the OpenAI key, the only route to frontier reasoning. Verified by direct call, not inference |
| **Agent Bricks** | **NOT available** — it is Beta, and Free Edition ships GA features only | Use Foundation Model APIs directly. Drop it from the plan |
| Genie | Available, UI plus Conversation API | Optional. Possible ambition play, not core |
| MLflow 3 | Tracing available | Core to Bet 4 |
| SQL warehouse | **One, fixed at 2X-Small** | Never compute verdicts at request time. Precompute |

The last row is the most important architectural constraint in the table.

## Architecture

**Precompute offline. The app only reads.**

```
India 10k (Unity Catalog Delta table)
        |
        v
  [ Adjudication ladder ]  <- notebook / job, runs once, MLflow-traced
  rung 0 presence -> rung 1 vocabulary -> rung 2 retrieval -> rung 3 entailment -> rung 4 referee
        |
        v
  verdicts Delta table          AI Search index
  (facility x capability,       (evidence passages,
   verdict, per-field marks,     gte-large-en embeddings)
   evidence spans, trace id)
        |
        v
  Databricks App (FastAPI)  <----> Lakebase (review decisions, notes, overrides)
        |                                   |
        v                                   v
  the existing HTML UI              calibration report (precision + intervals)
```

A live demo on a 2X-Small warehouse must not be doing 60,000 LLM calls. It reads a precomputed
table. Everything expensive happened beforehand, and MLflow holds the receipts.

## Language decision: Python

**Recommendation: Python for everything.** Both Python and Node/TypeScript are genuinely GA on
Databricks Apps, so this is not a support question — it is a gravity question.

Reasons, in order of weight:

1. **The data and AI SDKs are Python-first.** `databricks-vectorsearch`, `databricks.sdk`,
   PySpark, and MLflow 3 tracing (`@mlflow.trace`, `mlflow.openai.autolog()`) are all Python.
   Bet 4 depends on MLflow tracing, which has no comparable Node story.
2. **The adjudication ladder is data work, not app work.** It runs as a notebook or job over 10k
   rows. That is Python regardless of what the UI is written in. Choosing TypeScript means running
   two languages, not one.
3. **Databricks' TypeScript scaffolding (AppKit) is v0.** Databricks-owned and real, but pre-1.0.
   Not what to bet a deadline on.

**But we keep the front end we already have.** The concept mock is complete, polished, and
validated — bespoke evidence marks, expandable evidence rows, honest empty states. Rebuilding that
in Streamlit would lose the design and fight the framework.

So: **FastAPI serves the existing HTML/CSS/JS as a static page**, and the page fetches from a small
JSON API instead of its hardcoded `CATALOG` literal. No build step, no Node, no framework fight.
Flask/Gunicorn and FastAPI are both documented Databricks App patterns.

This means the TypeScript experience from TimeZyme is not wasted — the front end stays hand-written
HTML and JS, which is exactly the part that was already built.

## What transfers from TimeZyme

The honesty stance transfers. So, unexpectedly, does one real algorithm.

**Take:**
1. **Two-stage matching** — deterministic resolution separated from fuzzy matching; only the second
   stage is allowed to be uncertain. This is the spine of our ladder.
2. **Bidirectional containment and agreement scoring**, not one similarity number. Free-text
   facility descriptions are an open input space in exactly the way parsed bibliography titles were:
   the claim can be a substring of the evidence, or the evidence can be padded with noise.
3. **A high threshold with "not found" as the honest answer.** TimeZyme's matcher uses 0.9 and
   returns null below it, after a real regression where the best-available candidate at 0.56 was a
   different paper entirely. Same discipline here: never the closest look-alike.
4. **Asymmetric identifier upgrade** — a hard identifier that agrees promotes confidence; one that
   disagrees proves nothing and must not demote.
5. **Name the signal for what it grades.** TimeZyme's legend is titled "Source provenance," not
   "Confidence," and its weakest tier is "Limited," not "Low." Our labels already follow this.
6. **The failure floor** — past a threshold of component failures, fail loudly rather than ship what
   their code calls "bad output wearing a green checkmark."
7. **Separate infrastructure failure from evidentiary failure.** A missing credential must never
   render as "no evidence found."

**Avoid — these are TimeZyme's actual documented mistakes:**
1. **Silent rejection.** Their verifier deletes claims it dislikes, with only a server log. For us,
   a rejected claim plus its reason is the primary product output, not a deletion.
2. **A gap-disclosure UI that does not cover processing failures.** Their "Not found in this paper"
   state covers "no source" but not "we broke," so genuine failures vanish. We need three distinct
   states: found nothing / could not check / checked and refuted.
3. **Unknown rendering as confident.** Their citation chips only appear for medium and low
   confidence, so a `null` confidence renders identically to a verified exact match.
4. **Empty-string fallbacks for missing evidence.** A blank quote reads as normal. Make broken
   look broken.

## How we proceed

Ordered by dependency. Step 0 is complete; its measurements are in `docs/dataset-audit.md`.

0. **Completed: install and inspect the live dataset.** The table has 51 columns and 10,088 rows.
   `source_urls` is real. Descriptions are short, while capability, procedure, and equipment are
   richer JSON sentence arrays. Three malformed rows require quarantine. Source URLs are row-level
   provenance and do not map reliably to individual claim sentences.
1. **Build the ladder offline** in a notebook over 300 stratified claims, 50 for each target
   capability, MLflow-traced from the start. Add parse-and-quarantine before presence. Run lexical
   checks on individual sentence items using a small capability vocabulary, not the serialized JSON
   blob. Human-label support, refutation, irrelevant, and uncertain items, then measure what fraction
   the cheap rungs settle correctly and what fraction escalates.
2. **Materialize verdicts** to a Delta table, then build the AI Search index for evidence passages.
3. **Stand up the app**: FastAPI plus the existing HTML, reading the verdicts table. Deploy to Free
   Edition early — the brief says deploy early and demo on Free Edition, and 3-app plus 24-hour
   limits mean surprises should surface now, not on demo day.
4. **Wire Lakebase** for review decisions, so confirm and override persist.
5. **Close the calibration loop**: turn reviewer decisions into measured precision with intervals,
   and surface it in the app.
6. **Stretch, if time**: aggregate field-level states up to PIN code for the data-desert versus
   medical-desert map. Our per-facility model already produces the distinction, so the map is
   mostly presentation.

Git repo is required for submission and this project is not one yet. Initialize before step 1.

## Live risks

1. **Plain lexical matching is not enough.** Exact target vocabulary appears in two independent
   evidence fields for only 6-18% of target-claiming rows. Mitigation: a small, explicit medical
   vocabulary before LLM entailment, calibrated on the 300-claim labeled set. Do not hide the
   problem by selecting only facilities with rich descriptions.
2. **Provenance is row-level, not sentence-to-URL.** The source arrays do not align reliably with
   individual claim sentences. Mitigation: cite the exact row field and sentence, label URLs as the
   row's source set, and only assert page-level support after retrieving and verifying that page.
3. **Contradiction detection may underperform.** It is the hardest rung. Generic negative phrases
   are often extraction boilerplate or refer to another service. Mitigation: bind negation to the
   target capability and escalate ambiguous cases. A missed contradiction degrades gracefully to
   "limited record support," not a wrong confident answer.
4. **Free Edition rate limits on Foundation Model APIs are not documented for our tier.** Budget
   conservatively; the ladder is the primary defense.
5. **Calibration needs labels.** The 300-claim audit becomes the seed set. Reviewer confirms and
   overrides expand it after launch.
