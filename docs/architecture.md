# Architecture

## Context for anyone reading this cold

**The challenge.** Hack-Nation Challenge 04, *Data Legend* (Databricks x Virtue Foundation). We are
given 10,000 Indian medical facility records. Each record lists what the facility can do — "we have
an ICU." Nobody has ever checked whether those claims are true. Families travel hours to a hospital
and find the ICU was a claim, not a capability. The brief's framing: planners do not lack data, they
lack evidence they can act on.

**Our task.** We chose one of four offered tracks: **Facility Trust Desk**. Its required workflow,
quoted from the brief:

> Planner selects a capability (ICU, maternity, emergency, oncology, trauma, NICU) and region ->
> sees ranked facilities with trust signals -> expands any facility to inspect citations ->
> overrides the assessment with a note.

**What we are building.** A planner picks a capability and a region. They see facilities claiming it,
ordered by how well the rest of that facility's own record backs the claim up, with the supporting
or contradicting text shown. They can disagree and leave a note, which persists.

**How a record is judged.** Each record has four readable parts: description, capability list,
equipment list, procedure list. Each part is marked against the claim as *backs it*, *says nothing*,
*no data at all*, or *contradicts*. Those four marks combine into one verdict by a fixed rule.

The distinction that carries the product: **"says nothing" and "no data at all" are never merged.**
An empty field means we do not know. Collapsing them turns a paperwork gap into an apparent medical
desert, and telling them apart is what the challenge rewards most.

**What we are scored on.** Evidence and Trust 35%, Product Judgment 30%, Technical Execution 25%,
Ambition 10%. The brief says outright: "Since there is no ground truth, we value apps that
double-check their own work." That sentence is why this architecture is shaped the way it is.

**Hard constraints.**
- Must ship as a live Databricks App on **Free Edition**, demoed live.
- One SQL warehouse, fixed at 2X-Small — so verdicts are precomputed in batch, never at request time.
- Frontier models (Claude, Gemini) are rate-limited to zero in this workspace. Only open-weight
  models are reachable. Model calls are scarce and must be earned.

**What we already learned that constrains the design** (see `docs/dataset-audit.md`):
- The three claim fields are JSON arrays of extracted sentences, not prose. Items are judged
  individually.
- Descriptions are thin — median 115 characters, many are just "Hospital" or "Open 24 Hrs".
- Most negative language in the corpus is extractor boilerplate, not facilities contradicting
  themselves. A bare negative regex is not a contradiction detector.
- Three rows are corrupted and must be quarantined rather than judged.
- Source URLs exist but are row-level, not sentence-level. Citations are honest about that.

**Open question this design must survive.** We do not yet know what fraction of claims the cheap
checks can settle. If nearly everything escalates to a model, the economics fail on Free Edition.
That number is being measured now.

**Where to push back.** The riskiest bets are that cheap checks settle most cases, that real
contradictions are common enough to be worth first-class support, and that the indirection below
pays for itself in a hackathon timeframe. Critique those first.

---

Design first. The mission is that this flexes: new checks, new capabilities, new evidence sources,
new verdict rules — all without editing what already works.

## The one idea

Everything is **a check that is allowed to abstain.**

A check looks at one claim and one piece of evidence and returns either a finding or nothing.
Returning nothing means "I cannot settle this" — not failure, not absence. Checks never call each
other and never know what else is in the pipeline.

The ladder is then trivial: run checks in order, take the first finding, record which check produced
it. Adding a check is appending to a list. Reordering is data.

### The invariant that makes it correct

> A check may decide only when no more expensive check could overturn it. Otherwise it abstains.

This is what stops cheap-first from meaning sloppy-first. It is not theoretical: we tested the
available models on three cases and both got 2 of 3, failing the one where a record simply never
mentions the capability. The vocabulary check owns that case with certainty, so it decides it, and
the model never sees it. Cheap is also *more accurate* there — but only because the check knows the
boundary of what it can settle.

## Stages

Each stage is a seam. Data crosses stages as value objects, never as raw rows.

```
raw row
  -> Ingest          parse, validate, quarantine        -> Facility | Quarantined
  -> ClaimSource     which capabilities does it assert? -> [Claim]
  -> EvidenceSource  what can we read as evidence?      -> [EvidenceItem]
  -> Ladder          run checks until one decides       -> [Finding]
  -> FieldReducer    findings for a field -> one mark   -> Mark per field
  -> VerdictRule     marks -> one verdict               -> Verdict
  -> Sink            write verdicts + findings          -> Delta tables
```

Serving and review sit downstream and never re-run adjudication:

```
Delta tables -> App (read only) -> reviewer confirms/overrides -> Lakebase -> Calibration
```

## Core types

| Type | What it is |
|---|---|
| `Claim` | facility id + capability. The thing being verified |
| `EvidenceItem` | one readable unit: field name, text, position. One array item, or a description |
| `Finding` | mark or abstain, plus `check_id`, rationale, span, cost tier |
| `Context` | the claim, its evidence items, findings so far, anything enrichers added, remaining budget |
| `Mark` | per-field outcome: supports / silent / missing / conflicts / uncheckable |
| `Verdict` | per-claim outcome, derived from marks |

`Finding.check_id` is not optional. It is the receipt — every trust signal in the UI traces to the
check that produced it.

## The two protocols

**Deciders** answer. **Enrichers** add material for later checks and never answer.

```python
class Check(Protocol):
    id: str
    tier: Tier                      # FREE | CHEAP | METERED | RARE

    def applies(self, ctx: Context) -> bool: ...
    def examine(self, ctx: Context) -> Finding | None: ...   # None = abstain


class Enricher(Protocol):
    id: str
    tier: Tier

    def enrich(self, ctx: Context) -> Context: ...
```

Retrieval is an enricher: it fetches supporting and refuting passages into the context. The
entailment check then reads them. Neither knows the other exists.

## The ladder

```python
def adjudicate(ctx, pipeline, budget) -> Finding:
    for step in pipeline:
        if not budget.allows(step.tier):
            return Finding.unresolved("budget exhausted", ctx)
        if isinstance(step, Enricher):
            ctx = step.enrich(ctx)
            continue
        if not step.applies(ctx):
            continue
        if finding := step.examine(ctx):
            return finding
    return Finding.unresolved("no check could decide", ctx)
```

That is the whole engine. Everything else is a check.

`budget` is its own seam — a policy object. Free Edition rate limits are undocumented, so how
aggressively we escalate must be tunable without touching checks.

## What is data, not code

This is where the extensibility actually lives.

**Capabilities are data.** One spec per capability, loaded from file:

```yaml
name: ICU
synonyms:      [intensive care unit, critical care, ICU]
supporting:    [ventilator, mechanical ventilation, intensivist, central oxygen]
refuting:      [referred to, no intensive care, not maintained on site]
```

Adding dialysis or cardiac surgery is a new file. No code changes, no redeploy of logic.

**Pipelines are data.** An ordered list of check ids with tiers. Swapping vocabulary matching for
embedding similarity is a config edit, and both can run with the loser as a fallback.

**Evidence sources are registered.** Today: description, capability, equipment, procedure. The
dataset also has `numberDoctors` and `capacity`, which we ignore. Adding one is registering a
source, not rewriting adjudication — and it lands in the same `EvidenceItem` shape everything else
already handles.

## Extension points

| To add | You touch |
|---|---|
| A new check | One new class, one line in a pipeline config |
| A new capability | One YAML file |
| A new evidence field | One `EvidenceSource` registration |
| A different matching strategy | One check, swapped in config |
| A different verdict rule | One `VerdictRule` implementation |
| A different escalation budget | One policy object |
| A new model for entailment | Config on the existing check |

Nothing on that list requires editing a file that already works.

## What this buys beyond tidiness

1. **Checks are unit-testable in isolation.** No pipeline, no fixtures, no workspace.
2. **Every verdict is explainable by construction**, because the deciding check is recorded rather
   than reconstructed afterwards.
3. **The escalation rate becomes measurable per check**, which is the number the strategy hinges on.
   We can see exactly which check is failing to settle cases.
4. **A failed check is visible.** It abstains, and an unresolved finding is a real state — never
   silently rendered as "no evidence."

## Known cost

Indirection. For a hackathon this is only worth it because the checks genuinely churn: we have
already rewritten the vocabulary rung once after the dataset audit, and rungs 2 through 4 do not
exist yet. If the check set were settled, this would be over-engineering.

## Migration from what exists

Current `assess_field()` is the right behaviour in the wrong shape — five checks hardcoded as
if/else. It becomes five check classes with the logic moved, not rewritten. The 43 existing tests
pin the behaviour and must keep passing through the refactor.
