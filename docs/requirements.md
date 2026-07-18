# Requirements — Challenge 04, Databricks "Data Legend"

Source: `1784382653830-04-Databricks-Data-Legend.docx.pdf` (5 pages). Everything on this page is
quoted or closely paraphrased from that PDF. Nothing here is inferred. Read the PDF directly if a
detail matters; this is a working index, not a replacement.

Subtitle: *Building the Trust Layer for Indian Healthcare*.

## The problem, in the brief's own framing

Families travel hours to reach a hospital "only to discover the ICU was a claim, not a capability."
NGOs and public-health planners "do not lack data. They lack evidence they can act on."

The reasoning layer already exists (built by the Virtue Foundation and Databricks for Good). Our job
is the **product layer**: a live Databricks App that turns 10,000 messy records into decisions a
non-technical planner "can trust, defend, and save."

## Three core requirements

1. **The Evidence Engine.** Extract structure from the 10k messy records — free-text descriptions,
   capability claims, procedure logs, **and source URLs**. Every important output "must trace back
   to the facility text that supports it."
2. **The Trust Scorer.** There is no answer key, so the app must reason about confidence, not just
   retrieve keywords. The brief's worked example: a facility claiming Advanced Surgery with no
   anesthesiologist listed "should not rank the same as one with corroborating evidence across three
   fields." Flag suspicious or incomplete data and communicate uncertainty honestly.
3. **The Planner's Workflow.** Ship a Databricks App with a clear non-technical user journey. It
   **must persist user actions** — notes, overrides, shortlists, scenarios, or review decisions —
   "so work survives beyond a single session." Demo live on Free Edition.

## Mission tracks

"Choose ONE mission track. Nail its minimum workflow end-to-end. You are not expected to build all
four."

| Track | The question |
|---|---|
| **Facility Trust Desk** (ours) | Can this facility actually do what it claims? |
| Medical Desert Planner | Where are the highest-risk gaps, and how confident are we that they are real? |
| Referral Copilot | Where should a patient or coordinator actually go? |
| Data Readiness Desk | What must be fixed before this dataset can be trusted for planning? |

### Our track's minimum workflow, quoted exactly

> Planner selects a capability (ICU, maternity, emergency, oncology, trauma, NICU) and region ->
> sees ranked facilities with trust signals -> expands any facility to inspect citations ->
> overrides the assessment with a note.

Those six capabilities are the literal set named in the brief. Treat them as the fixed vocabulary.

## Evaluation criteria

| Weight | Criterion | What it actually asks |
|---|---|---|
| **35%** | Evidence and Trust | Outputs grounded in row-level citations. Communicates uncertainty honestly, distinguishing strong evidence from weak claims **and data deserts from medical deserts**. "Since there is no ground truth, we value apps that double-check their own work." |
| **30%** | Product Judgment | Is the user clear? Is the workflow intuitive for a non-technical NGO planner? Does it solve a real decision problem, "not just showcase technology behind a chat box"? |
| **25%** | Technical Execution | Works reliably in a live demo on Free Edition. Are Apps, serverless compute, Vector Search, and Lakebase used well? |
| **10%** | Ambition | Went beyond the minimum workflow meaningfully: multi-track integration, self-correction loops, crisis mapping, or real-impact alignment. |

## Stretch goals

1. **Agentic traceability.** Beyond row-level citations: show the exact sentence and the reasoning
   step that produced each trust signal. Extraction -> scoring -> ranking, "with receipts at every
   step." Hint: use MLflow 3 Tracing to visualize it.
2. **Self-correction loops.** A Validator step that cross-references extracted claims against known
   medical standards or internal consistency rules, so the primary logic is not hallucinating
   capabilities that do not exist.
3. **Dynamic crisis mapping.** Overlay trust-weighted findings on a map of India by PIN code, and
   "visually separate 'no hospitals here' from 'we don't know what's here.'"
4. **Real-impact bonus.** Solve one of the Databricks for Good open questions marked "could have" or
   "won't have" and call it out in the demo.

## Open research questions the brief poses

These are stated as unsolved by the organizers, which makes them scoring opportunities.

- **Confidence scoring.** How do you quantify trust when there is no ground truth? Can statistics-
  based methods create prediction intervals so planners know what is solid versus speculative?
- **Claims vs evidence.** "Fields like capability, procedure, and equipment are claims to verify,
  not facts." What separates a facility whose description corroborates its ICU claim from one that
  lists ICU with no supporting text anywhere?
- **The data desert problem.** With capacity at 25% coverage and doctor counts at 36%, how does the
  app stop a sparse region from looking like a medical desert when it might just be a data desert?

## Hard constraints

- **Databricks Free Edition.** Optimized for it; explicitly "do not use an enterprise or paid
  organizational workspace for your submission." Build locally where it helps, but deploy early and
  demo on Free Edition.
- **Submission:** a Git repo **and** a live Databricks App.
- **Demo:** be ready to give a one-minute demo covering the user, workflow, technical approach, and
  key tradeoffs.

## Named tech stack

| Layer | Product |
|---|---|
| App surface | Databricks Apps — submission must ship as a live, deployable app |
| Data intelligence | Agent Bricks, for foundation model training and serving |
| Agentic engineering | Genie, for autonomous multi-step data tasks |
| Observability | MLflow 3, for agent observability and trace cost tracking |
| Vector DB | Mosaic AI Vector Search, for retrieval across 10k rows |
| Persistence | Lakebase, for user notes, overrides, shortlists, and scenarios |

## Dataset — India 10k

10,000 medical facilities across India. Structured metadata plus deep unstructured notes across
**51 columns**. The brief instructs: "Treat extracted evidence fields as noisy claims, not ground
truth."

| Field | Coverage |
|---|---|
| description | 100% |
| capability | 99.7% |
| procedure | 92.5% |
| equipment | 77.0% |
| numberDoctors | 36.4% |
| capacity | 25.2% |
| yearEstablished | 47.8% |

Access requires a Databricks account. The brief also links a Virtue Foundation schema document and
the prompts and pydantic models used to create the data.

**Not yet verified by us:** we have not opened the dataset. Every field-level statement above comes
from the brief, not from inspecting real rows. The 51-column schema is unexamined.
