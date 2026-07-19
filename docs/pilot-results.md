# Blind-labelled free-check pilot

**Status:** `complete_preliminary`

**Evidence gate:** NOT GREEN — assistant-labelled rehearsal; human validation is still required.

**Decision:** REHEARSAL ONLY: keep the free-check demo, but do not claim human-validated safety or scale the model path.

60 evidence items were labelled in complete six-capability waves (36 development, 24 holdout; minimum 60).
The sealed reviewer was read from the Delta label rows, not supplied to this report command.
This is a timeboxed assistant review, not human ground truth, final accuracy, or proof of current clinical capability.

## Holdout safety action

The first sealed holdout score caught one false trauma support from the generic `injury` case class. The terms `injury` and `injuries` were removed, so that class now abstains.
This was the only holdout-driven change and it narrowed the rule to abstention; no support term was added.
The initial failure is manually recorded history; current metrics and reviewer provenance are recomputed.

## Model-call economics

The free checks fully settled 0 of 120 queued claims. 120 (100.0%) would require one model bundle call.
At the same rate, approximately 10505 of 10505 live asserted claims would escalate.

## Split results

### Development (n=36)

| Capability | Check | Coverage (95% CI) | Abstention rate | Precision (95% CI) | Errors |
|---|---|---:|---:|---:|---:|
| ICU | presence | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| ICU | vocabulary | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| maternity | presence | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| maternity | vocabulary | 16.7% (3.0 to 56.4%) | 83.3% | 100.0% (20.7 to 100.0%) | 0 |
| emergency | presence | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| emergency | vocabulary | 16.7% (3.0 to 56.4%) | 83.3% | 100.0% (20.7 to 100.0%) | 0 |
| oncology | presence | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| oncology | vocabulary | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| trauma | presence | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| trauma | vocabulary | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| NICU | presence | 0.0% (0.0 to 39.0%) | 100.0% | — (—) | 0 |
| NICU | vocabulary | 16.7% (3.0 to 56.4%) | 83.3% | 100.0% (20.7 to 100.0%) | 0 |

### Holdout (n=24)

| Capability | Check | Coverage (95% CI) | Abstention rate | Precision (95% CI) | Errors |
|---|---|---:|---:|---:|---:|
| ICU | presence | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| ICU | vocabulary | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| maternity | presence | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| maternity | vocabulary | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| emergency | presence | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| emergency | vocabulary | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| oncology | presence | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| oncology | vocabulary | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| trauma | presence | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| trauma | vocabulary | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| NICU | presence | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |
| NICU | vocabulary | 0.0% (0.0 to 49.0%) | 100.0% | — (—) | 0 |

## Contradiction prevalence

Target-bound refutation labels: 0 of 60. Generic negative-language hits: 0.
These counts stay separate because generic negatives often describe unrelated services or boilerplate.

## Frozen manifests

- Queue: `e8e4ef3b40b101361fefaca411ec1738a28b2f02a679121bb9c1d19b2d4bd1e5`
- Development: `e64c3fdcc09d8b709a63380d390c675e3b249aea5fb158bff198dd2713edcf8a`
- Holdout: `67efe3a8fac08b6c99bde16d3e995c577be805a869c5734f7487e04d826c8032`
- Rule configuration: `bb98071069889f7b91e230cf8e969679dcdef6e3ea849069fdb7cac35ead2727`

This preliminary pilot is not equivalent to the audit's proposed 300-claim experiment.
