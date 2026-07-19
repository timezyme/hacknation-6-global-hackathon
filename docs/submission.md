# Submission answers

Copy each block into the matching field on the submission form.

---

## What problem does your project solve? What pain point are you addressing?

10,000 Indian medical facility records list what each hospital can do: ICU, maternity,
emergency. Nobody has checked whether those claims are true. Families drive hours to a hospital
and find the ICU was a claim, not a capability. Planners have plenty of data but no way to tell
which records they can act on.

Two things make this hard. There is no ground truth, so any confident score is made up. And an
empty record is not a "no". Treating missing paperwork as a missing hospital sends help to the
wrong place.

---

## Who benefits from your solution? Who is your main target group?

The main user is the non-technical planner at an NGO or health department deciding where to
send patients, staff, or funding. They pick a capability and a region, see which facilities'
own records back their claims, and read the evidence behind every call. No SQL, no chat box.

Downstream, the people who benefit are families acting on those decisions: fewer six-hour
drives to an ICU that only exists on paper. Clinicians and data teams benefit too, because the
checks are replaceable. Someone who knows ICUs better than we do can plug in a better check
without reading our code.

---

## Short description of the project/solution

Facility Trust Desk answers one question: does a facility's record support what it claims?
Each claim (ICU, maternity, emergency, oncology, trauma, NICU) runs through a pipeline of
checks, cheapest first. A free vocabulary check settles the easy cases; a model only sees the
hard ones. Each of the record's four fields gets a grade, a fixed rule turns the grades into a
verdict, and the app shows the receipt: which field said what and which check decided. Blank
fields never count as a "no", failures are never hidden as "no evidence", and there is no
invented confidence score. Planners confirm or override verdicts, and that feedback is stored
per check, so anyone can swap in a better check and see whether it helped. Built on Databricks
Free Edition: batch jobs write Delta tables, a read-only FastAPI app serves them, reviews go
to Lakebase.

---

## How do you solve the problem? What are your main functionalities?

We treat every capability a facility lists as a claim to check, not a fact. For each claim we
read the four fields of its record (description, capability, equipment, procedure) and grade
each one: backs it, says nothing, blank, contradicts, or unreadable. A fixed rule turns those
grades into one verdict. No model ever writes the verdict, so the label cannot drift from the
evidence under it.

Main functionalities:

1. Ranked search: pick a capability and region, see facilities ordered by how well their own
   record backs the claim. Low-data facilities are shown separately as unassessed, not buried
   at the bottom.
2. Receipts: expand any facility to see the per-field grades, the exact sentences cited, and
   which check made each call.
3. Cheap-first checking: a free vocabulary check settles the clear cases; an open-weight model
   only sees what the free checks cannot decide. Every check can abstain instead of guessing.
4. Review loop: planners confirm or override any verdict with a note. Decisions persist in
   Lakebase and are counted per check.
5. Swappable checks: adding or replacing a check is one file and one config line. The review
   counts show whether the new check did better.
6. Second opinions: every decision is re-checked by an independent method, and the receipt
   says whether the checks agree, disagree, or could not double-check. The app also shows
   per-check pilot numbers with confidence intervals, so a planner can see how much to trust
   each check before trusting a verdict.

---

## What makes your project better or different from existing solutions?

Most tools in this space either search the records or score them with a model and print a
confidence number. Both dodge the real problem: there is no ground truth, so nobody can prove
their scoring is right, including us. We built for that instead of around it.

1. No black-box score. The verdict comes from a fixed, visible rule over per-field evidence
   grades, and every call shows which check made it and what sentence it read.
2. "Says nothing" and "blank" are never merged. One is a record that doesn't mention an ICU;
   the other is missing paperwork. Confusing them is exactly how aid gets misrouted.
3. Our own failures are a first-class result. A parse error shows as "could not check", never
   quietly as "no evidence".
4. The checks are the replaceable part, not the product. A doctor can swap in a better ICU
   check with one file, and the review counts show whether it beat ours.
5. The cheap check is not just cheaper. We tested the reachable models and they mislabel silent
   records as contradictions. The free vocabulary check owns exactly that case, so the pipeline
   is more accurate than the model alone.

---

## How did you technically implement the solution? What technologies do you use?

Everything runs on Databricks Free Edition. The dataset is the Virtue Foundation facilities
table (10,088 rows) in Unity Catalog. A Python batch job reads it, quarantines malformed rows,
runs each asserted claim through the check pipeline, and writes the results to Delta tables:
facility index, verdicts, receipts, and a run manifest. Publication is all-or-nothing. A
partial run can never go live, and the previous good run is kept for rollback.

The checks are plain Python (src/trustdesk): a presence check, a vocabulary check with
per-capability term lists and negation that must bind to the target, and an entailment check
that calls a Databricks-served open-weight model (Llama 3.1 8B, Qwen by config; the frontier
models are rate-limited to zero on Free Edition, which we found by testing, not in the docs).
Checks are configured units behind one protocol. Order and membership live in config, and the
model sits behind a small client interface so tests run against an in-memory fake. The model
check ships disabled: we measured its throughput and it could not finish the full batch with
headroom, so config turns it off and its cases show as unresolved. That is the honest mode the
design planned for.

Two more pieces round it out. A referee re-examines every decision with a method independent
of the one that decided it, and the receipt says whether the checks agree, disagree, or could
not double-check. Disagreement is displayed, never hidden. And Mosaic AI Vector Search adds
similar-facility context to ranked receipts: the most similar records from a
capability-relevant subset of the dataset, labelled as comparison context, not verification.

The app is FastAPI on Databricks Apps, serving a plain HTML/JS front end. It only reads the
published Delta tables. Nothing is adjudicated while a planner waits, because Free Edition
gives one 2X-Small warehouse. Confirm/override decisions are written to Lakebase (managed
Postgres) with a snapshot of what the system said at the time.

Tooling: Python 3 with uv, pytest, MLflow tracing on the batch runs, git with a protected
main branch.

---

## What have you achieved? What values does your solution bring?

We built a working end-to-end trust layer over 10,088 real facility records: from raw table to
a live app where a planner picks a capability and region, sees ranked facilities, reads the
evidence behind every verdict, and can override it. All 10,505 asserted claims are assessed
and published atomically, every decision carries a second opinion or an honest "not
double-checked" label, and the deployed app has survived a cold restart with reviews intact. Along the way we audited the real dataset
(three corrupted rows, JSON-array claim fields, boilerplate negatives) and tested the reachable
models. Both findings shaped the design: the models mislabel silent records as contradictions,
so the free checks own exactly the cases the models get wrong.

The value is that decisions become defensible. A planner no longer forwards a raw claim or a
black-box score. They can show the sentence, the rule, and the check behind every verdict, and
say "not enough data" when that is the truth. Empty paperwork is never mistaken for a missing
hospital.

The longer-term value is the loop. Every confirm and override is stored per check, so the
system's judgment can be measured and replaced by anyone with better domain knowledge. The
brief asked how you quantify trust without ground truth. Our answer: you don't fake it. You
make every judgment traceable, replaceable, and measured against reviewer feedback.

---

## Any additional details, notes, or information about your project that doesn't fit in the structured sections above?

Built solo. The whole process is in the open in the repo: running notes, decisions, dead ends,
and corrections in NOTES.md and docs/, including the calls we got wrong and reversed (we
first advised against an external model key, then testing showed the in-workspace frontier
models are unreachable).

Two details worth knowing:

1. The honesty rules are enforced in code, not just described. Boilerplate like "No specific
   procedures listed" reads as no data, a negative only refutes a claim it actually binds to,
   and a regression test proves a new check can be added without touching the pipeline.
2. The demo degrades honestly. If the model check fails qualification it is disabled in config
   and its cases show as unresolved. A partial batch can never replace a published run.

The brief's open research questions, answered in the shipped app:

1. Quantifying trust without ground truth: the blind-labelled pilot reports per-check coverage
   and precision with 95% confidence intervals, and the app shows them in its measurements
   panel. Where we have no measurement, the verdict says so instead of inventing a number.
2. Claims vs. evidence: the four field grades are exactly the line between a corroborated ICU
   claim and a bare listing. A claim backed across fields outranks one that lists ICU with no
   supporting text anywhere.
3. The data desert problem: "says nothing" and "blank" are separate grades, never merged and
   never shown as a low rank. A sparse region looks like missing paperwork, not a missing
   hospital, because that is what the record actually says.

Links:
- Repo: https://github.com/timezyme/hacknation-6-global-hackathon
- Architecture walkthrough: https://claude.ai/code/artifact/f024a24c-c01c-4567-8c26-ad8f7f1e1159
- UI concept: https://claude.ai/code/artifact/6c0e58d1-a3ac-45fe-b854-75f9d5481e9f

---

## What was your most fun moment during the hackathon?

Learning Databricks from scratch (Unity Catalog, Delta, Lakebase, Apps) and coming up with a
solution for the Trust Desk track that actually felt right. I started the day never having
touched the platform and ended it with a working design.
