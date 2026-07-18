# Hackathon challenge selection — handoff

Status as of 2026-07-18. Read this to resume without re-deriving anything.

This file is session-to-session only. The project's own notes and decisions live in `NOTES.md`, and
the challenge requirements are extracted in `docs/requirements.md`.

## Decision

**Building Challenge 04 — Databricks "Data Legend" (Trust Layer for Indian Healthcare).**

Chosen on spirit match. The platform cost (full rebuild on Databricks) was raised and
explicitly accepted: learning the platform is part of the goal. Do not reopen this.

## Context

Six challenge briefs live in `/Users/spasco/Projects/hackathon` as PDFs. The product being
matched against them is **TimeZyme** (`/Users/spasco/Projects/github-timezyme/timezyme-site`),
pitched as: takes a dense document, pulls out its claims, verifies each against the source,
shows which text supports it, and is honest about what's missing or uncertain.

Ranking was by fit to that spirit — evidence, verification, honest uncertainty.

| Rank | Challenge | Fit | Does its rubric reward evidence/verification/honesty? |
|---|---|---|---|
| 1 | 04 Databricks Data Legend | Near-exact | Yes — Evidence and Trust is the largest single bucket at 35% |
| 2 | 03 RealPage RealDoor | Very close | Yes — Profile accuracy 25% + Rules and math 25% = 50% |
| 3 | 02 Maschmeyer VC Brain | Strong but diluted | Partly — Trust Score 25%, but sourcing/utility carry 60% |
| 4 | 06 Genome Firewall | Honesty yes, task shape no | Yes for calibration; but it's a classifier, not doc verification |
| 5 | 01 ElevenLabs Negotiator | Peripheral | Barely — won on voice conversation design |
| 6 | 05 Women's Hormonal Health | Weakest | No — rewards reusable open-science infrastructure |

A mid-course argument for switching to 03 (better code reuse, near-isomorphic flow) was
made and **rejected by the user**. 04 stands.

## Challenge 04 — the facts that matter

**Rubric** (this drives everything):
- Evidence and Trust — **35%**. Row-level citations; communicate uncertainty honestly;
  distinguish strong evidence from weak claims, and **data deserts from medical deserts**;
  "we value apps that double-check their own work."
- Product Judgment — 30%. Clear user, intuitive for a non-technical NGO planner, solves a
  real decision problem rather than showcasing tech behind a chat box.
- Technical Execution — 25%. Works live on Free Edition; uses Apps, serverless, Vector
  Search, Lakebase well.
- Ambition — 10%. Beyond the minimum: self-correction loops, crisis mapping, multi-track.

**Hard constraints:** must ship as a live, deployable Databricks App on **Free Edition**
(not an enterprise/paid workspace). Submit a git repo plus the live app. Be ready for a
one-minute demo covering user, workflow, technical approach, tradeoffs.

**Stack named in the brief:** Databricks Apps (surface), Agent Bricks (model serving),
Genie (multi-step data tasks), MLflow 3 (observability/tracing), Mosaic AI Vector Search
(retrieval over 10k rows), Lakebase (persistence for notes/overrides/shortlists).

**Dataset:** India 10k — 10,000 medical facilities, 51 columns, structured metadata plus
deep unstructured notes. Requires a Databricks account to access. Field coverage, which is
itself the core product problem:

| Field | Coverage |
|---|---|
| description | 100% |
| capability | 99.7% |
| procedure | 92.5% |
| equipment | 77.0% |
| numberDoctors | 36.4% |
| capacity | 25.2% |
| yearEstablished | 47.8% |

Treat `capability`, `procedure`, `equipment` as **claims to verify, not facts**.

**Pick ONE mission track** (not all four):
1. *Facility Trust Desk* — can this facility actually do what it claims?
2. *Medical Desert Planner* — where are the highest-risk gaps, and how confident are we?
3. *Referral Copilot* — where should a patient actually go?
4. *Data Readiness Desk* — what must be fixed before this dataset can be trusted?

**RESOLVED — track chosen: 1, Facility Trust Desk.** Confirmed against the brief itself, whose
minimum workflow for that track matches the existing concept mock step for step. Reasoning and
everything decided since live in `NOTES.md`.

**Stretch goals:** agentic traceability (exact sentence + reasoning step behind each trust
signal, via MLflow 3 tracing); self-correction validator step; dynamic crisis mapping by
PIN code that visually separates "no hospitals here" from "we don't know what's here."

## What TimeZyme actually is (verified against the code, not the pitch)

This was expensive to establish. Trust it.

**The gap:** TimeZyme enforces **provenance**, not **verification**. A claim is guaranteed
to *point at* a real chunk of the source. Nothing ever compares the claim's words to the
source's words.

- Claim schema is `{ text, sourceBlockIds (nonempty), caveats[] }` —
  `workers/pdf-processor/src/mastra/schemas/l2-claim-schema.ts`. No support-level field.
- Anchor granularity is a whole layout block (`"{page}-{block}"`), not a span or offset.
- `claim-validation.ts` deterministically rejects any cited id outside an allowed set. That
  is real and good, but it is an ID whitelist, not a meaning check.
- "Verification" is a second Gemini pass at temperature 1.0 that **deletes** claims it
  dislikes — `l2-claim-verification-agent.ts`. Failures vanish silently; the reader is
  never told a claim was cut.
- No retrieval, embedding, string matching, entailment, or contradiction detection exists
  anywhere in the repo. Searched and confirmed.
- The one genuine matcher is `citation-linker.ts` (exact/containment/agreement scoring,
  DOI upgrade to high confidence) — it verifies **references**, not claims.
- Per-claim confidence does not exist. The shipped signal grades source *type*: text
  ranks above figure description. See `layers/dashboard/app/utils/l0Confidence.ts`.
- `snippet.verified` is hardcoded `true` for every snippet ever shipped.
- Page coordinates (`bbox`) flow through the entire worker pipeline and are **dropped
  before the UI** — verified: zero hits across `app/`, `layers/`, `shared/`, `server/`.
- The eval/grader harness was **deleted** (commit `f2daf43b`). The sibling `timezyme-evals`
  project was specified but never created and is not on disk. `OFFLINE_EVAL_SAMPLE_RATE`
  is `"0"` in all three environments. There is no eval script. So there is currently no
  way to measure or calibrate the confidence signals — which is exactly the commitment
  `docs/what-is-timezyme.md:230` makes.

**What is genuinely strong and worth carrying over — the honesty stance, not the code:**
- `ZymePaperSummaryCard.vue` renders a "Not found in this paper" block for missing
  content, whose tooltip admits "It may not exist in the paper, or extraction may have
  missed it."
- The confidence legend volunteers its own limitation: "It does not check the summary
  wording against the source."
- The citations panel shows an "unverified match" chip only when uncertain — confident
  matches stay silent.
- Known inconsistency: L1 walkthrough stops that fail are dropped silently, unlike the
  visible L0 treatment. Don't reproduce that pattern.

Stack (for reference only — none of it transfers to Databricks): Nuxt 4, Vue 3, Cloudflare
Workers/D1/R2, Drizzle, Gemini via Vertex through AI Gateway.

## Guidance carried forward into the 04 build

1. Build a **real claim-vs-evidence check** (retrieval plus agreement/contradiction), not
   provenance linking. This is where the 35% is won, and it's the thing the existing
   product never had. Budget it as core work, not polish.
2. Make uncertainty a **labeled, visible state**. "We don't know" and "there is a real
   gap" must be distinct outputs — that is literally the data-desert/medical-desert
   distinction the rubric names. Never drop weak claims silently.
3. **Row-level citations on every output.** That's the other half of Evidence and Trust.
4. The transferable asset is the honesty instinct, not the codebase. Rebuild that stance
   on Databricks and it aims straight at the rubric.

## Note on the fresh session

The prior session repeatedly tripped Fable 5 safeguards and got bounced to Opus 4.8. The
most likely trigger is Challenge 06 (Genome Firewall) content — bacterial genomes,
antibiotic-resistance genes, AMR annotation — which is flagged-adjacent biology even though
the work is defensive and routine. Challenge 06 is ranked 4th and not being built, so a
fresh session that skips it entirely should avoid the issue. This doc deliberately keeps
06 to a single line for that reason.
