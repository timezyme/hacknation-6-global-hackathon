# India 10k dataset audit

Live inspection of the Databricks Marketplace dataset on 2026-07-18. This replaces assumptions
from the challenge brief with measurements from the installed table.

## Bottom line

The dataset can support Facility Trust Desk, but the first strategy overstated what plain lexical
matching can decide.

- The three claim fields contain useful sentence-level material.
- The `description` field is usually short and often generic.
- Exact target-language agreement across two independent fields is uncommon.
- Source URLs exist, but they are row-level sources, not reliable sentence-to-URL citations.
- Three malformed rows show column-shift corruption and must be quarantined before analysis.

Keep the adjudication ladder. Add a parse-and-quarantine rung, operate on individual JSON-array
items, and replace plain containment with capability-specific vocabularies. Use the LLM for
semantic or negated cases the vocabulary cannot settle.

## What was inspected

The Marketplace listing was installed into Unity Catalog as
`virtue_foundation_dais_2026.virtue_foundation_dataset.facilities`. Queries ran through the SQL
Statements API against the project's Free Edition warehouse. No dataset rows or credentials were
copied into the repository.

The live table has 51 columns and 10,088 rows, not exactly 10,000. It contains 10,077 distinct
`unique_id` values, leaving 11 duplicate-ID rows beyond the distinct count.

## Live field coverage and shape

The brief-reported coverage is close, but slightly higher than the current table.

| Field | Non-empty rows | Live coverage | Typical shape |
|---|---:|---:|---|
| `description` | 10,008 | 99.2% | Plain text; median 115 characters |
| `capability` | 9,947 | 98.6% | JSON string array; median 19 items |
| `procedure` | 9,218 | 91.4% | JSON string array; median 11 items |
| `equipment` | 7,683 | 76.2% | JSON string array; median 3 items |
| `source_urls` | 9,970 | 98.8% | JSON string array; median 10 URLs |

Descriptions are not the deep evidence layer implied by the brief. Of all rows:

- 925 descriptions are 30 characters or shorter.
- 3,399 are 80 characters or shorter.
- 6,477 are 160 characters or shorter.
- Repeated descriptions include `Hospital`, `Open 24 Hrs`, `Clinic`, and `Dental Clinic`.

The richer material is in the JSON arrays. Their items are already extracted claim sentences, such
as equipment or procedure statements. They must be parsed and assessed item by item. Searching the
serialized JSON blob would blur independent evidence and extraction boilerplate together.

## Cheap lexical pilot

The pilot treated a facility as claiming a target capability when its `capability` array contained
that capability or a close synonym. It then searched `description`, `procedure`, and `equipment`
for the same target vocabulary.

`Any other match` means at least one of those three fields repeated the target vocabulary.
`Strong lexical support` means at least two did, matching the mock's three-of-four rule once the
claim field itself is counted.

| Capability | Claiming rows | Any other match | Strong lexical support |
|---|---:|---:|---:|
| ICU | 2,123 | 45.1% | 8.4% |
| Maternity | 3,213 | 56.8% | 15.9% |
| Emergency | 3,295 | 35.0% | 6.3% |
| Oncology | 1,521 | 60.4% | 17.8% |
| Trauma | 1,188 | 50.2% | 7.5% |
| NICU | 602 | 39.0% | 7.3% |

This is a conservative baseline, not a final accuracy score. It misses meaningful related evidence
such as ventilators supporting ICU without repeating `ICU`. That is exactly why plain containment
cannot be the decisive lexical rung. A small, explicit vocabulary can add relationships such as
ICU -> ventilator, central oxygen, critical-care monitor, and intensivist while preserving an
explainable receipt.

The current verdict rule can still cheaply label many rows `Limited record support`, because that
label means the other populated fields do not repeat the claim. It must not be described as proof
that support is absent. Semantic cases should escalate rather than receive a confident negative.

## Contradiction warning

Roughly 13-16% of target-claiming rows contain generic negative or referral language somewhere in
the other fields. A manual sample showed that most obvious hits are extraction boilerplate such as
`No specific procedures listed in the provided content` or negatives about an unrelated service.

Therefore a generic `no`, `not available`, or `referred` regex is not a contradiction detector.
Contradiction logic must bind the negative statement to the target capability. Ambiguous negatives
belong in LLM entailment and referee steps.

## Provenance limit

`source_urls` is real and almost always populated. However, the live schema does not expose a
reliable mapping from each capability, procedure, or equipment sentence to one URL:

- `source_types` and `source_ids` have equal array lengths on only 4,520 rows.
- Claim-array lengths rarely equal source-array lengths.
- Rows often contain many URLs from multiple sites and sometimes similarly named facilities.

The honest citation contract is therefore:

1. Quote the exact sentence item from the facility row.
2. Identify its field and facility row.
3. Present `source_urls` as the row's source set, not as sentence-level proof.
4. Only name a particular URL as supporting that sentence after fetching and verifying the page.

Row-level citations satisfy the core brief. Sentence-to-web-page verification is additional work
and should not be implied by the first version.

## Data-quality guardrail

Three rows fail JSON parsing across the claim columns and visibly contain values shifted into the
wrong fields. For example, a facility-name column contains a phrase fragment while claim columns
contain a UUID, coordinates, or booleans.

Add a deterministic pre-rung:

1. Parse `capability`, `procedure`, `equipment`, and `source_urls` as arrays.
2. Require a plausible facility name and identifier.
3. Quarantine malformed rows as `Could not check: malformed record`.
4. Never convert a parsing failure into `Not enough data`.

## Decision and next experiment

The evidence model survives, with changes. Do not narrow to only rich-description facilities; that
would hide the dataset's real uncertainty. Build the next pilot from 300 claims, stratified as 50
per target capability. Human-label each candidate evidence item as support, refutation, irrelevant,
or uncertain. Use that set to:

- tune the six capability vocabularies;
- measure how many claims presence plus vocabulary can settle correctly;
- measure how many must escalate to LLM entailment;
- seed the first calibration report.
