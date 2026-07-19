# Project notes

Running notes for Challenge 04, Databricks "Data Legend". This is where conversation notes,
thinking, and decisions get gathered as they happen. Not a plan and not a strategy doc — just the
record of what we said and settled on, so nothing has to be re-derived.

Companion docs:
- `docs/architecture.md` — how the system is built to flex. **Start here.**
- `docs/requirements.md` — what the challenge brief demands, extracted from the PDF.
- `docs/dataset-audit.md` — what the real data actually looks like. Overrides any assumption.
- `docs/strategy.md` — the bets and why. Partly predates the audit; read it after the audit.
- `docs/verdict-contract.md` — table schemas. Stale in places, flagged inline.
- `HANDOFF.md` — session-to-session resume notes. Different purpose; not a project doc.

---

## The thesis — read this before anything else

**We are not shipping the right answer. We are shipping the thing that lets people add better
answers.**

1. There is no answer key. Nobody knows the true state of these 10,000 hospitals.
2. So any single way of checking a claim is one opinion, including ours.
3. Claiming our checks are correct would be dishonest — the exact thing the challenge penalises,
   since it says outright that it values apps which double-check their own work.
4. The honest move is to make the checking method **swappable and measurable**, and always show
   which check decided.
5. Our six capability vocabularies and five checks are **example content, not the product**. Each
   check is an independent, replaceable unit that can be swapped, reordered, or removed without
   touching the others. The product is the slot they plug into, not what we happened to put in it.

**Why "measurable" matters as much as "swappable."** Anyone can claim modularity. Without
measurement a swap is unevaluable — you can replace a check and have no idea whether you made
things worse. The review loop supplies the yardstick, so someone can prove their check beats ours
rather than just asserting it.

**What this means concretely.** A doctor who knows more about ICUs than we do should be able to add
a better ICU check by writing one file, not by reading our code.

**How we say it in the demo.** "We don't know if our checks are right. So we built it so you can
replace them, and we show you which check made every call."

---

## 2026-07-18

### Track: Facility Trust Desk

We are building the **Facility Trust Desk** track. Its question: *can this facility actually do what
it claims?*

This was effectively settled before the discussion started — a concept mock already existed titled
"Facility Trust Desk," and the brief's minimum workflow for that track matches it step for step:

> Planner selects a capability (ICU, maternity, emergency, oncology, trauma, NICU) and region ->
> sees ranked facilities with trust signals -> expands any facility to inspect citations ->
> overrides the assessment with a note.

The reasoning for preferring it over the alternatives, for the record: the unit of work is one
facility's claims, and the job is to verify each against evidence and be honest about uncertainty.
That is where the 35% Evidence and Trust bucket lives. Medical Desert Planner was the runner-up —
it owns the data-desert-versus-medical-desert distinction most visibly, but there verification is an
input and aggregation is the product, so the per-claim double-checking the rubric rewards gets
buried under a map. Referral Copilot and Data Readiness Desk fit worse.

Note the desert distinction is not lost by choosing Trust Desk: it falls out as two verdict states
on the same claim — "unverifiable, data missing" versus "contradicted or genuinely absent."

### The mock

A clickable concept mock preceded the app; the shipped UI in `app/index.html` grew from it.

The core idea worth protecting: **a four-state evidence mark per field**, checked across
Description, Capability, Equipment, Procedure.

| Mark | Meaning |
|---|---|
| Backs the claim | This field says something that supports it |
| Says nothing | Field has content, but never mentions this capability |
| No data at all | Field is empty. Absence of proof, not proof of absence |
| Contradicts | This field says the opposite of the claim |

The split between "Says nothing" and "No data at all" is the data-desert versus medical-desert
distinction pushed down to the field level. That is the single most valuable idea in the mock.

Second most valuable: low-data facilities are **structurally separated**, not sorted to the bottom.
They sit below a dashed divider reading "not ranked low — they are unassessed. A blank record is a
gap in the paperwork, not a verdict on the hospital."

### Decisions made

1. **Verdict is derived from the marks, never hand-written.** Four steps, in order: any field
   contradicts -> Conflicting evidence; fewer than 3 of 4 fields hold data -> Not enough data; 3 or
   more fields back the claim -> Strong record support; anything else -> Limited record support.
   Deriving it means a label can never drift from the evidence beneath it. The rule is shown in the
   UI, because the rule *is* the trust story.
   - Pleasant accident: the brief's own Trust Scorer example uses "corroborating evidence across
     three fields" as the bar. Our threshold matches.
2. **Status labels state what the system knows, not what is true.** Strong record support / Limited
   record support / Conflicting evidence / Not enough data. The earlier labels ("Corroborated",
   "Thin claim") overstated — "Corroborated" sounds like the hospital was checked, and "Thin claim"
   subtly blames the facility for missing paperwork.
3. **No scalar confidence score.** We rejected adding "Confidence: Medium" alongside the four-state
   model. There is no eval harness to calibrate what "Medium" would mean, so it would be invented
   precision — dishonest in exactly the way the 35% bucket punishes. The four marks plus the verdict
   already express uncertainty, and they are traceable.
4. **The product claim is narrowed.** Headline is "Does a facility's record support what it claims?"
   with a persistent banner: record-based assessment, current clinical capability not independently
   confirmed. The old headline ("Can this facility actually do what it claims?") promised clinical
   reality that records cannot support.
5. **Every control does something.** Capability chips and the region filter re-derive the whole view.
   Previously they were decorative, so clicking a chip produced a visible mismatch — the kind of
   thing a judge finds in the first ten seconds of a live demo.

### Corrections to earlier thinking

- **Source URLs exist in the dataset.** I said earlier I could not confirm this. The brief's Evidence
  Engine requirement lists record contents as "free-text descriptions, capability claims, procedure
  logs, and source URLs." So citations should carry the source URL, not just a record ID. The mock
  does not do this yet.
- **The mock's expandable evidence view was never missing.** It was mistaken for a gap during review.
  It was already the strongest thing in the mock. What was genuinely absent inside it: the claim
  stated as a sentence, the record ID, and a "still unknown" line. All three added.

### Open threads

Status as of the end of the day. Struck items are resolved further down this file.

- ~~**Contradiction detection is the whole technical risk.**~~ Partly superseded. Still the hardest
  check, but the dataset audit found most negative language is extractor boilerplate, so the real
  contradiction rate is unknown and may be low. Whether it stays a first-class check is now a
  strategy question, not an assumption.
- **Nothing double-checks itself yet.** Still open. The rubric says "we value apps that double-check
  their own work," and a Validator step is stretch goal 2. Under the current thesis this is not one
  feature but the measurement half of swappable-and-measurable.
- **Persistence is a hard requirement.** Still open. User actions must survive beyond a session.
  Needs Lakebase.
- ~~**We have never opened the dataset.**~~ Done. See the live audit below and
  `docs/dataset-audit.md`.
- ~~**Not a git repo yet.**~~ Done. Initialized; commits go on branches, a hook blocks `main`.

### Setup state as of today

- `.env` holds `DATABRICKS_ACCESS_TOKEN`, so an account exists.
- `.mcp/DatabricksMCP/` is a local checkout of a Databricks MCP server, pinned at commit
  `191a5bcd`. `scripts/run-databricks-mcp` launches it with `DATABRICKS_MCP_ACCESS_MODE=read-only`.
- That server is registered in one local tool config only; no Databricks tools are available in
  a session elsewhere until it is registered there too.
- `.gitignore` correctly excludes `.env` and the vendored MCP checkout, but the project root is not
  a git repository yet.

---

## 2026-07-18 (later) — grounding against the real workspace

Strategy now lives in `docs/strategy.md`. Notes below are the findings behind it.

### Workspace is real and verified

Credentials in `.env` work. `GET /api/2.0/preview/scim/v2/Me` returns 200 as
`stephen.pasco@gmail.com`, active. One SQL warehouse: "Serverless Starter Warehouse", 2X-Small.

These APIs all return 200 (empty, nothing created yet): vector-search endpoints, database instances
(Lakebase), apps, genie spaces. Unity Catalog has a `workspace` catalog.

**Model serving is the pleasant surprise.** Already READY in-workspace, no setup needed:
`databricks-claude-opus-4-8`, `databricks-claude-sonnet-5`, `databricks-gemini-3-5-flash`,
`databricks-gpt-oss-120b`, plus embeddings `databricks-gte-large-en`, `databricks-bge-large-en`,
`databricks-qwen3-embedding-0-6b`. Nineteen endpoints total.

### OpenAI key: present, kept as a fallback, not in the primary path

`OPENAI_API_KEY` was added to `.env` (gitignored, verified). Live check returns HTTP 200 with 131
models, including the `-pro` reasoning tiers (`gpt-5.5-pro`, `gpt-5.4-pro`, `o3-pro`), the
deep-research models, and `text-embedding-3-large`.

I originally advised against it. Amending that, because I got one part wrong and the model list
settles another:

- **Correction to my earlier reasoning.** I said an external model would cost us in the 25%
  Technical Execution bucket. Overstated — that criterion names "Apps, serverless compute, Vector
  Search, Lakebase" specifically, and does **not** name model serving. So an external model is
  cheaper than I implied, as long as those four are used well.
- **It cannot back the AI Search index.** Free Edition supports Delta Sync only, and Delta Sync
  takes its embeddings from a Databricks serving endpoint. Direct Vector Access, which would let us
  supply our own vectors, is explicitly unsupported. So `text-embedding-3-large` is unusable for
  retrieval here regardless of preference. Embeddings must be `databricks-gte-large-en` or similar.
- **It does not buy referee independence.** The decorrelation argument for an out-of-family second
  opinion evaporates on inspection: Databricks already serves Anthropic (Opus 4.8, Sonnet 5),
  Google (Gemini 3.5 Flash), OpenAI (the `gpt-5-6-luna`/`terra`/`sol` family), Alibaba (Qwen), and
  Meta (Llama). We can build a genuinely multi-provider referee without leaving the workspace.

**What it is genuinely good for:** a quota escape hatch if Free Edition FM API rate limits bite
(they are undocumented for our tier), local prompt iteration without burning workspace quota, and
the `-pro` reasoning tiers if step 1 turns up a class of contradiction cases the in-workspace models
consistently fail. That last one is an empirical question, not an assumption.

**Decision: keep it, do not design around it.** The primary path stays in-workspace.

### Decision: Python, with the existing HTML front end

Both Python and Node/TypeScript are genuinely GA on Databricks Apps, so this was not a support
question. Python wins on gravity: the Databricks SDKs, PySpark, and MLflow 3 tracing are all
Python-first, and the adjudication work is data work that would be Python no matter what the UI is
written in. Choosing TypeScript would mean running two languages.

But we keep the mock's front end. **FastAPI serves the existing HTML/CSS/JS**, and the page fetches
JSON instead of using its hardcoded `CATALOG` literal. No build step, no framework fight, and the
design we already validated survives intact.

### Agent Bricks is not available on Free Edition

The brief names it in the primary tech stack, but it is Beta and Free Edition ships GA features
only. Confirmed by the Free Edition limitations page (Knowledge Assistant listed as unsupported)
and a Databricks community manager. Foundation Model APIs substitute completely. **The brief's
stack list is partly aspirational — do not treat it as a checklist.**

### Free Edition limits that actually shape the build

- Apps: 3 per account, auto-stop 24h after start or redeploy, restartable. Restart before demoing.
- AI Search (the new name for Vector Search): 1 endpoint, 1 search unit, **Delta Sync only** —
  Direct Vector Access is unsupported. Fine for 10k rows.
- Lakebase: 1 project, scale-to-zero. Available and is our persistence layer. An app resource
  binding auto-injects `PGHOST`/`PGUSER`/etc.
- Model serving: pay-per-token only, no provisioned throughput. FM API rate limits are **not
  documented for the Free tier** — budget conservatively.
- SQL warehouse: one, fixed at 2X-Small. This is why verdicts get precomputed rather than computed
  at request time.

### The core strategic idea

Superseded framing, kept for the record: this section used to present the adjudication ladder as
the strategic idea. It is not. It is the mechanism. See "The thesis" at the top of this file — the
strategic idea is that **checks are swappable and measurable**, and the ladder is simply how they
are arranged.

Restated in the right order:

1. **The slot is the product.** Every check is an independent unit that abstains when it cannot
   decide. Adding, reordering, or replacing one touches nothing else. Our six vocabularies and five
   checks are example content that ships in the slot, not the thing being sold.
2. **The ladder is the arrangement.** Parse -> presence -> vocabulary -> retrieval -> entailment ->
   referee, first check that can decide wins, and every verdict records which check decided it. That
   record is what makes a swap traceable — you can see exactly which check made which call.
3. **The review workflow is the measurement.** Everyone will treat "persist reviewer decisions" as
   storage. We treat confirms and overrides as labels, then publish measured precision per verdict
   with Wilson score intervals. That is what turns swappability from a claim into something
   provable: replace a check, rerun, see whether the number moved.

Point 3 answers the brief's own open research question about quantifying trust without ground truth,
and closes the exact hole we found in TimeZyme — confidence signals with no harness to calibrate
them.

Still unproven: how many claims the cheap checks settle. The 300-claim labeled pilot measures it.

### TimeZyme: one real algorithm transfers, not just the stance

Expected to salvage only the honesty instinct. Actually found a genuine matcher worth adapting in
`workers/pdf-processor/src/mastra/citations/citation-linker.ts`:

- Two-stage design: deterministic key resolution separated from fuzzy entity matching, and only the
  second stage is permitted to be uncertain.
- Three signals rather than one score: bidirectional `containment`, `agreement`, and exact equality.
- Threshold 0.9 on containment, below which the answer is "not found" — never the closest
  look-alike. Driven by a real regression where a 0.56 best-available match was a different paper.
- Exclusive assignment, best-score-first: one piece of evidence cannot support two competing claims.
- Asymmetric identifier upgrade: a matching DOI promotes confidence, a mismatching one proves
  nothing and must not demote.

Anti-patterns confirmed and to be avoided: their verifier drops rejected claims silently; their
"Not found in this paper" state does not cover processing failures, so genuine breakage vanishes;
a `null` confidence renders identically to a verified match; missing evidence falls back to an
empty string that reads as normal.

### The Databricks MCP is still not wired into this environment

It is registered in one local tool config only, so this environment gets no Databricks tools.
I worked around it by calling the REST API directly with the `.env` credentials, which is how
everything above was verified. Per `docs/learnings.md`: installed and configured is not the same as
connected and verified. To use it here it needs registering in this environment's MCP config.

(Note on naming: this is the **Databricks** MCP. Dataplex is a Google Cloud product.)

---

## 2026-07-18 (interim work, while dataset exploration runs elsewhere)

### The frontier models are blocked. I was wrong to wave off the OpenAI key.

Listing serving endpoints shows every model `READY`. **That listing lies.** Actually calling them:

| Endpoint | Result |
|---|---|
| `databricks-claude-opus-4-8` | **403** — "temporarily disabled due to a Databricks-set rate limit of 0" |
| `databricks-claude-sonnet-5` | **403** — same |
| `databricks-gemini-3-5-flash` | **403** — same |
| `databricks-meta-llama-3-3-70b-instruct` | 200, ~0.6s |
| `databricks-qwen3-next-80b-a3b-instruct` | 200, ~0.9s |
| `databricks-gemma-3-12b`, `databricks-meta-llama-3-1-8b-instruct` | 200 |
| `databricks-gpt-oss-120b` / `-20b` | 200 but response shape differs, did not parse. Unresolved |
| `databricks-gte-large-en`, `-bge-large-en`, `-qwen3-embedding-0-6b` | 200, 1024 dims |

The pattern: **proprietary frontier models are rate-limited to zero on Free Edition; open-weight
models work.** This is the undocumented Free Edition limit I flagged as a risk, now confirmed by
direct test rather than inference.

**Consequence: adding the OpenAI key was the right call and my advice against it was wrong.** I
argued Databricks already served Opus 4.8 and Sonnet 5 so an external key added nothing. It served
them in the listing only. Frontier reasoning is not reachable in-workspace at all, so the key is
now the only route to it.

**Good news: embeddings all work.** That was the load-bearing dependency — AI Search Delta Sync
requires a Databricks embedding endpoint, and all three respond with 1024 dimensions. Retrieval is
unblocked.

### The ladder routes around a real model failure, not just cost

Ran three hand-built cases (contradiction, support, silent) against the reachable models:

| Case | llama-3.3-70b | qwen3-next-80b |
|---|---|---|
| Description refutes the ICU claim | CONTRADICTS, correct | CONTRADICTS, correct |
| Description corroborates it | SUPPORTS, correct | SUPPORTS, correct |
| Description simply never mentions ICU | **CONTRADICTS, wrong** | **CONTRADICTS, wrong** |

Both score 2/3, and both fail the *same* case: they over-call contradiction when a record is merely
silent. A nursing home that just does not mention ICU gets labelled as refuting the claim.

That is exactly the failure that would destroy the product, because `silent` versus `conflicts` is
the distinction the whole design rests on.

**But rung 1 already decides the silent case and never escalates it.** No capability term appears,
so it is marked `silent` for free and the model never sees it. The ladder only escalates when a
mention *and* refuting language co-occur — which is precisely where these models are accurate.

So the ladder turns a 2/3 model into a system correct on all three, and the justification is no
longer just cost. It is that the cheap rung is *more accurate* than the expensive one on the case
it handles. Worth saying exactly that in the demo.

### Model plan, revised

- **Rung 3 (entailment):** `databricks-meta-llama-3-3-70b-instruct` or
  `databricks-qwen3-next-80b-a3b-instruct`. Both reachable, both sub-second, both Databricks-native
  which protects the 25% Technical Execution bucket.
- **Rung 4 (referee):** OpenAI, via the key. Now genuinely independent, because the in-workspace
  frontier models are unreachable. This is the only path to frontier reasoning on disagreements.
- **Embeddings:** `databricks-gte-large-en`. In-workspace, required by Delta Sync anyway.

### What got built

Repo initialized, `.env` confirmed untracked. Commits go on branches; a hook blocks `main`.

- `src/trustdesk/marks.py` — the five marks, five verdicts, and the derivation rule in one place.
- `src/trustdesk/lexicon.py` — capability vocabularies, refutation patterns, boilerplate patterns,
  sentence splitting. Deliberately excludes bare "referral", since a *referral hospital* receives
  referrals and that supports a claim rather than refuting it. Regression-tested.
- `src/trustdesk/ladder.py` — parse, presence and vocabulary checks. Returns `mark=None` to mean
  abstain, which is a decision to defer, never a failure.
- 43 tests passing.
- `docs/verdict-contract.md` — the table schema. Written before the data landed, so it is stale on
  source URLs (row-level, not sentence-level), field shapes (arrays, not prose), and the quarantine
  state. Needs reconciling.
- `docs/architecture.md` — the design that makes the thesis real: one `Check` protocol with
  abstention, enrichers separate from deciders, capabilities and pipelines as data rather than code.
  **This is the doc a new contributor should read first.**

**Known gap between the design and the code.** `docs/architecture.md` describes checks as
independent pluggable units. `ladder.py` currently hardcodes them as if/else flow inside
`assess_field()`. The behaviour is right, the shape is not — adding a sixth check means editing that
function. Refactoring to the protocol is agreed but deliberately not done yet; the 43 tests exist to
pin behaviour through that change.

### Live dataset audit completed

The Virtue Foundation Marketplace listing is now installed in Unity Catalog as
`virtue_foundation_dais_2026`. The live `facilities` table has 51 columns, 10,088 rows, and 10,077
distinct `unique_id` values. Full measurements are in `docs/dataset-audit.md`.

The evidence model survives, but the dataset changes its implementation:

- `description` is plain text with a median length of 115 characters. It is often too generic to be
  the main evidence source.
- `capability`, `procedure`, and `equipment` are JSON string arrays containing richer extracted
  claim sentences. Evidence marks must operate on individual items, not whole serialized fields.
- Exact target vocabulary appears in two independent fields for only 6-18% of target-claiming rows.
  Plain containment is not enough. The lexical rung becomes a small, explicit vocabulary for each
  of ICU, maternity, emergency, oncology, trauma, and NICU.
- Generic negative phrases are noisy. `No specific procedures listed` is a missing-data statement,
  not a contradiction of every capability. Negation must bind to the target or escalate.
- `source_urls` exists, but source arrays do not reliably map individual claim sentences to URLs.
  The UI may cite the exact row sentence and show the row's source set. It must not claim a specific
  page supports a sentence until that page is fetched and verified.
- Three obviously column-shifted rows fail claim-field JSON parsing. A parse-and-quarantine rung now
  precedes presence, and malformed records render as `Could not check`, never `Not enough data`.

Next experiment: label 300 claims, 50 per target capability, as support, refutation, irrelevant, or
uncertain. Use that set to tune the vocabularies, measure cheap-rung accuracy and escalation rate,
and seed calibration.

## Where the story continues

This log covers the exploration phase, in order. Execution then moved to
`docs/winning-demo-plan.md`, whose per-phase status lines are the rest of the story. The labelled
pilot's results are in `docs/pilot-results.md`, the shipped design in `docs/architecture.md`, and
the demo was frozen on 2026-07-19 — restart and verification steps in `docs/demo-runbook.md`.
