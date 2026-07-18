# Verdict contract

> **Superseded 2026-07-18.** The shipped contract now lives in [architecture.md](architecture.md)
> ("The contract" section); execution order lives in [winning-demo-plan.md](winning-demo-plan.md).
> Where this file disagrees — four marks instead of five (no `failed`), four verdicts instead of
> five (no `could not check`), rung numbers as identity, the cross-product claim population — the
> architecture wins. Kept unchanged below for history.

The interface between the offline adjudication ladder and the live app. Written before the dataset
was opened, deliberately: fixing the contract now lets the data work and the app work proceed
without colliding, and makes disagreements surface as contract changes rather than silent rework.

Status: **draft, pending dataset confirmation.** The open questions at the bottom are the parts
only the dataset can settle.

## Vocabulary

Two enums carry the whole product. Nothing else may express uncertainty.

### Mark — the state of one field with respect to one claim

| Mark | Meaning | Decided by |
|---|---|---|
| `supports` | This field says something that backs the claim | rung 1 or 3 |
| `silent` | Field has content, but never mentions this capability | rung 1 |
| `missing` | Field is empty. Absence of proof, not proof of absence | rung 0 |
| `conflicts` | This field says the opposite of the claim | rung 3 or 4 |

`silent` versus `missing` is the data-desert versus medical-desert distinction at field level. They
must never be merged, and neither may be rendered as "low".

### Verdict — the state of one claim across all four fields

Derived from the marks by a fixed rule, never written by a model. Order matters.

1. Any field is `conflicts` -> `conflicting`
2. Fewer than 3 of 4 fields have data -> `not_enough_data`
3. 3 or more fields are `supports` -> `strong_support`
4. Otherwise -> `limited_support`

The rule lives in exactly one place (`src/trustdesk/marks.py`) and is displayed to the user. If the
rule and the marks ever disagree, that is a bug in the deriver, not a judgement call.

## Tables

### `verdicts` — Delta, Unity Catalog. One row per facility per capability.

| Column | Type | Notes |
|---|---|---|
| `facility_id` | string | Dataset row key. **Column name TBD** |
| `capability` | string | One of: `ICU`, `maternity`, `emergency`, `oncology`, `trauma`, `NICU` |
| `verdict` | string | `strong_support` / `limited_support` / `conflicting` / `not_enough_data` |
| `mark_description` | string | One of the four marks |
| `mark_capability` | string | " |
| `mark_equipment` | string | " |
| `mark_procedure` | string | " |
| `decided_at_rung` | int | 0-4. The highest rung that had to fire |
| `escalated` | boolean | True if any field needed rung 3 or above |
| `why` | string | Plain-language explanation, shown verbatim in the UI |
| `still_unknown` | string | What this record cannot tell us, shown verbatim |
| `trace_id` | string | MLflow trace for the full adjudication |
| `computed_at` | timestamp | |

Four flat mark columns rather than an array, because the app filters and the SQL warehouse is
2X-Small. Field order is fixed: description, capability, equipment, procedure.

### `evidence` — Delta. One row per field per verdict. Four rows per verdict, always.

Always four, even when a field is `missing`. A missing row must be visible as missing, not absent
from the table — absence is how gaps get silently dropped.

| Column | Type | Notes |
|---|---|---|
| `facility_id` | string | |
| `capability` | string | |
| `field` | string | `description` / `capability` / `equipment` / `procedure` |
| `mark` | string | |
| `text` | string | The full field text as it appears in the record. Empty when `missing` |
| `span_start` | int | Character offset of the supporting or contradicting span. Null if none |
| `span_end` | int | " |
| `source_url` | string | From the record if present. Null otherwise |
| `rung` | int | Which rung produced this mark |
| `rationale` | string | One sentence. For rung 3+, the model's stated reason |

`span_start`/`span_end` are what let the UI highlight the exact sentence rather than the whole
field. That is stretch goal 1 (agentic traceability) and it is cheap if we capture offsets at
adjudication time. It is expensive to retrofit, so capture them from the first run.

### `review_decisions` — Lakebase Postgres. Written by the app, at request time.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `facility_id` | text | |
| `capability` | text | |
| `decision` | text | `confirmed` / `overridden` |
| `note` | text | Null when confirmed |
| `system_verdict` | text | The verdict at the time of review. **Snapshot, do not join live** |
| `reviewer` | text | From the app's user context |
| `created_at` | timestamptz | |

`system_verdict` is snapshotted deliberately. If the ladder is re-run and a verdict changes, we must
still know what the human was disagreeing with. Joining live would silently rewrite history and
destroy the calibration set.

## Why this shape

Three properties matter more than convenience:

1. **Every mark is attributable to a rung.** "Why did you say that?" resolves to a rung plus a span,
   with no model call needed for the majority of cases.
2. **Missing is represented, never omitted.** The `evidence` table always has four rows. A gap that
   is absent from the data becomes a gap that is invisible in the UI.
3. **Review decisions are immutable and snapshotted.** They are not just persistence — they are the
   labeled set that calibrates the system, so they must survive a re-run of the ladder unchanged.

## Open questions for the dataset work

These block finalizing the contract. Answers needed, in rough priority order:

1. **What is the row key?** Exact column name and whether it is stable and unique.
2. **What are the four evidence fields actually called**, and do their contents match the brief's
   description? `description`, `capability`, `procedure`, `equipment` are the brief's names, not
   confirmed column names.
3. **Is there a source URL column?** The brief's Evidence Engine requirement mentions source URLs.
   If it exists, name it. If not, we drop `source_url` and say so in the UI rather than faking it.
4. **How are capabilities expressed in the `capability` field?** Free text, a delimited list, a
   controlled vocabulary? This determines whether matching the six capability terms is trivial or
   is itself a extraction problem.
5. **Geography columns:** what is available for region filtering — state, district, city, PIN? The
   mock currently filters by district.
6. **How long and how rich is `description` in practice?** Median and distribution of length. If
   most are two lines, rung 1 will rarely fire and the ladder's economics change materially.
7. **Do refutation patterns actually appear?** Search for referral-out language: "referred to",
   "not available", "no ... on site", "closed", "under renovation". If contradictions are vanishingly
   rare, the `conflicting` state is a demo feature rather than a real one, and we should know that
   before building rung 4.

Question 6 and question 7 are the ones that decide whether the ladder works. Everything else is
plumbing.
