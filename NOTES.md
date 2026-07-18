# Project notes

Running notes for Challenge 04, Databricks "Data Legend". This is where conversation notes,
thinking, and decisions get gathered as they happen. Not a plan and not a strategy doc — just the
record of what we said and settled on, so nothing has to be re-derived.

Companion docs:
- `docs/requirements.md` — what the challenge brief actually demands, extracted from the PDF.
- `HANDOFF.md` — session-to-session resume notes. Different purpose; not a project doc.

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

Live at https://claude.ai/code/artifact/6c0e58d1-a3ac-45fe-b854-75f9d5481e9f

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

- **Contradiction detection is the whole technical risk.** The interesting cases only work if the
  system reads free text like "all critical and ventilator-dependent cases are referred to Patna"
  and recognizes it as the opposite of an ICU claim. That is genuine entailment checking, not
  retrieval. Budget it as core work.
- **Nothing double-checks itself yet.** The rubric says outright "we value apps that double-check
  their own work," and a Validator step is stretch goal 2. No self-check exists in the mock.
- **Persistence is a hard requirement.** The brief says user actions must survive beyond a session.
  The mock keeps overrides in memory only. Real build needs Lakebase.
- **We have never opened the dataset.** Every field-level assumption comes from the brief's coverage
  table, not from real rows. The 51-column schema is unexamined. This should happen early — the
  whole evidence model assumes those four fields carry usable text.
- **Not a git repo yet.** The brief requires submitting one.

### Setup state as of today

- `.env` holds `DATABRICKS_ACCESS_TOKEN`, so an account exists.
- `.mcp/DatabricksMCP/` is a local checkout of a Databricks MCP server, pinned at commit
  `191a5bcd`. `scripts/run-databricks-mcp` launches it with `DATABRICKS_MCP_ACCESS_MODE=read-only`.
- That server is registered in `.codex/config.toml` — **for Codex, not for Claude Code**. No
  Databricks tools are available in a Claude Code session until it is registered there too.
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

The adjudication ladder: presence -> lexical -> retrieval -> LLM entailment -> referee, where the
first rung that can decide, decides, and every verdict records which rung fired. Most verdicts end
up explainable with no model call at all, which is both cheaper and more defensible.

Second idea, the one we think is genuinely novel: **the review workflow is the calibration loop.**
Everyone will treat "persist reviewer decisions" as a storage requirement. We treat confirms and
overrides as labels, then publish measured precision per verdict state with Wilson score intervals.
That answers the brief's own stated open research question about quantifying trust without ground
truth, and it closes the exact hole we found in TimeZyme.

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

### The Databricks MCP is still not usable from Claude Code

It is registered in `.codex/config.toml` only, so a Claude Code session gets no Databricks tools.
I worked around it by calling the REST API directly with the `.env` credentials, which is how
everything above was verified. Per `docs/learnings.md`: installed and configured is not the same as
connected and verified. To use it here it needs registering in Claude Code's MCP config.

(Note on naming: this is the **Databricks** MCP. Dataplex is a Google Cloud product.)
