# Facility Trust Desk

**Does a facility's record support what it claims?**

Built for Hack-Nation Challenge 04, *Data Legend* — Databricks x Virtue Foundation.

> **[Watch the full app tour — 4 min, every screen and panel](https://youtu.be/m6RIa9r1fLw)** ·
> [Architecture](docs/architecture.md) ·
> [Demo runbook](docs/demo-runbook.md) ·
> [Submission notes](docs/submission.md)

---

## The challenge

The dataset holds 10,088 Indian medical facility records. Each one lists what it can do:
"we have an ICU." Nobody has ever checked whether those claims are true.

A family drives six hours to reach a hospital and finds the ICU was a claim, not a capability.
The brief's framing: NGO and public-health planners "do not lack data. They lack evidence they
can act on." The reasoning layer already exists; the challenge is the **product layer** — a live
Databricks App that turns messy records into decisions a non-technical planner can trust,
defend, and save.

The brief offers four mission tracks and asks for exactly one, nailed end to end.

## Our track: Facility Trust Desk

Its minimum workflow, quoted from the brief:

> Planner selects a capability (ICU, maternity, emergency, oncology, trauma, NICU) and region ->
> sees ranked facilities with trust signals -> expands any facility to inspect citations ->
> overrides the assessment with a note.

Why this track: the unit of work is one facility's claims, and the job is verifying each against
evidence while being honest about uncertainty. That is where the rubric's largest bucket — 35%
for Evidence and Trust — lives.

## The thesis

**We are not shipping the right answer. We are shipping the thing that lets people add better
answers.**

There is no answer key for 10,000 hospitals, so every checking method is one opinion — including
ours. The brief says outright that it values apps which double-check their own work. So the
checks are built to be **swappable and measurable**: each one is an independent unit that can be
added, removed, or replaced with one file and one config line, every verdict records which check
decided it, and reviewer feedback accumulates per check so a swap can be evaluated instead of
asserted. A doctor who knows more about ICUs than we do should improve the system by writing a
file, not by reading our code.

## How a record is judged

Each record has four fields we can read: its description, its capability list, its equipment
list, and its procedure list. Claims descend a pipeline of configured checks, cheapest first —
presence, vocabulary, and an optional batched open-weight model for entailment (built, gated,
and shipped **disabled** in config: the labelled pilot showed the free checks carry this
corpus). A check that cannot safely decide **abstains** and passes the item along; a check that
breaks records a processing failure, never a silent "no evidence."

Each field ends up with one of five grades:

| Grade | Meaning |
|---|---|
| Backs it | This field says something that supports the claim |
| Says nothing | The field has content, but never mentions this capability |
| Blank | The field is empty. Absence of proof, not proof of absence |
| Contradicts | The field says the opposite of the claim |
| Unreadable | We could not process it. Our failure, recorded, never hidden |

**"Says nothing" and "blank" are never merged.** That distinction is the data-desert versus
medical-desert split — the difference between "there is no hospital here" and "we have no
information about here" — pushed down to the field level.

The four grades combine into one verdict — strong record support, limited record support,
conflicting evidence, not enough evidence, or could not check — by a fixed rule we wrote.
Never a model. The same four grades always produce the same answer, so a label can never drift
from the evidence beneath it. The rule is shown in the UI, because the rule *is* the trust
story.

Two more things ride along on every ranked receipt:

1. **A referee second opinion.** Every decision is re-examined by a method independent of the
   one that made it — vocabulary decisions are corroborated only by wording the deciding
   lexicon could not itself have matched. The referee never changes a verdict; agreement,
   disagreement, and "could not double-check" are all displayed honestly.
2. **Similar-facility context** from a Mosaic AI Vector Search index (a capability-relevant
   subset of the corpus) — comparison context only, clearly labelled: similarity is not
   verification.

## Architecture

The full design, its contract, and every tradeoff live in [docs/architecture.md](docs/architecture.md).
The shipped pipeline is:

**ingest and quarantine -> configured checks -> deterministic reduction and verdict -> atomic
Delta publication -> read-only app.**

```mermaid
%%{init: {"flowchart": {"wrappingWidth": 560}}}%%
flowchart LR
    subgraph BATCH["BATCH — runs once, ahead of time"]
      direction TB
      UC[("Unity Catalog<br/>10,088 facility rows")] --> RUN["Check pipeline<br/>asserted claims only"]
      RUN --> OUT[("Delta tables<br/>facility index · verdicts · receipts · manifest")]
      LLM["Llama endpoint<br/>(optional — ships disabled)"] -.-> RUN
      MLF["MLflow traces"] -.-> RUN
    end

    subgraph LIVE["LIVE — while the planner waits"]
      direction TB
      API["FastAPI app<br/>reads + review writes only"] --> LB[("Lakebase<br/>reviews")]
    end

    OUT --> API

    classDef store fill:#12161c,stroke:#4a515e,color:#e7eaef
    class UC,OUT,LB store
```

Nothing is adjudicated while someone waits. Free Edition gives one 2X-Small warehouse, so every
claim is settled in batch, published atomically (a partial run can never become active), and the
app only reads verdicts and writes reviews.

## The app

**A narrated in-depth tour of the deployed app — every screen and panel:
[youtu.be/m6RIa9r1fLw](https://youtu.be/m6RIa9r1fLw)** (also committed at
[artifacts/trustdesk-tour.mp4](artifacts/trustdesk-tour.mp4)).

Deployed live on Databricks Free Edition (workspace login required; see
[docs/demo-runbook.md](docs/demo-runbook.md) for restart and verification). The planner's whole
journey: pick a capability and region, see facilities ranked by how well their own record backs
the claim, expand any row to read the receipt — the per-field grades, the exact cited
sentences, which check made each call, the attempt trail, the referee's second opinion, and
similar facilities for context — then confirm or override with a note. Reviews persist in
Lakebase across restarts.

Two ideas in it are load-bearing:

1. **A per-field evidence grade**, shown for all four fields, so the verdict is inspectable
   rather than a black-box score.
2. **Low-data facilities are structurally separated, not sorted to the bottom.** They sit below
   a divider reading: not ranked low — unassessed. A blank record is a gap in the paperwork, not
   a verdict on the hospital.

## Honesty rules the system enforces

1. A parsing failure is quarantined as "could not check." It never degrades into "not enough
   evidence," which would blame the facility for our bug.
2. Extractor boilerplate ("No specific procedures listed in the provided content") reads as no
   data, never as a contradiction.
3. A negative only counts against a claim when it binds to that claim. "12-bed intensive care
   unit. Dental services not available." does not refute the ICU claim.
4. Nothing is silently dropped: quarantined rows publish their failure reason as a receipt.
5. No invented confidence score. There is no calibration set yet, so we do not print a number
   that implies there is.
6. Reviewer decisions are stored as snapshotted feedback per check — never described as measured
   accuracy, because a reviewer reads the same row we do.

## Repository layout

```
src/trustdesk/   the check pipeline: marks.py (grades and the verdict rule) · lexicon.py ·
                 checks + config loading · batch.py (precompute + atomic publication) ·
                 referee.py (second opinions) · similar.py (vector-search context) ·
                 sink.py (Databricks writes)
app/             the deployed FastAPI app: main.py · repositories.py (batch + Lakebase
                 stores) · index.html
config/          checks.toml — which checks run, in what order, and the referee settings
scripts/         batch runner · vector index builder · deployed smoke test · pilot tooling
tests/           163 tests: unit, production-adapter, and end-to-end
docs/            architecture (start here) · demo runbook · submission · pilot results
NOTES.md         exploration-phase decisions and findings, in the order they happened
```

## Running the tests

```sh
uv run pytest -q
```

163 tests pass (81% line coverage), alongside `ruff` and `mypy` gates and a Playwright
end-to-end suite. A deployed smoke test (`scripts/smoke_demo.py`) verifies the live app's whole
workflow, including a review write that survives restarts.

## Who built this

Stephen Pasco ([timezyme](https://github.com/timezyme)) — built solo during the hackathon, with
the working notes, decisions, and dead ends kept in the open in `NOTES.md` and `docs/`. The
project's one rule applies to its own history too: show the receipts.
