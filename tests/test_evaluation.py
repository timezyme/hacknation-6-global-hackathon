"""Behavior tests for the blind, timeboxed free-check pilot."""

from __future__ import annotations

from collections import Counter

import pytest

from trustdesk.check_presence import PresenceCheck
from trustdesk.check_vocabulary import VocabularyCheck
from trustdesk.evaluation import (
    BlindLabel,
    EvidenceLabel,
    PilotExample,
    PilotQueue,
    build_queue,
    evaluate_pilot,
    evidence_gate_status,
    validate_label_extension,
    validate_labels,
)
from trustdesk.ladder import ClaimEvidence, run_checks
from trustdesk.lexicon import CAPABILITIES
from trustdesk.models import Claim, FacilityRecord


def candidate_claims(count_per_capability: int = 25) -> tuple[tuple[FacilityRecord, ...], tuple[Claim, ...]]:
    records: list[FacilityRecord] = []
    claims: list[Claim] = []
    for capability in CAPABILITIES:
        for index in range(count_per_capability):
            record_key = f"record-{capability}-{index:02d}"
            records.append(
                FacilityRecord(
                    record_key=record_key,
                    facility_id=f"facility-{capability}-{index:02d}",
                    name=f"{capability} Facility {index:02d}",
                    description=f"Evidence description {index}",
                    capability=(capability,),
                    procedure=(f"Procedure evidence {index}",),
                    equipment=(f"Equipment evidence {index}",),
                    source_urls=(),
                    region="Bihar",
                )
            )
            claims.append(Claim(record_key, capability))
    return tuple(records), tuple(claims)


def test_queue_is_reproducible_balanced_and_frozen_before_labelling():
    records, claims = candidate_claims()

    queue = build_queue(records, claims, seed="phase-4-frozen")
    repeated = build_queue(tuple(reversed(records)), tuple(reversed(claims)), seed="phase-4-frozen")

    assert queue == repeated
    assert len(queue.examples) == 120
    assert Counter(example.capability for example in queue.examples) == Counter(
        {capability: 20 for capability in CAPABILITIES}
    )
    assert Counter((example.capability, example.split) for example in queue.examples) == Counter(
        {
            **{(capability, "development"): 12 for capability in CAPABILITIES},
            **{(capability, "holdout"): 8 for capability in CAPABILITIES},
        }
    )
    assert [example.queue_position for example in queue.examples] == list(range(1, 121))
    assert all(
        {example.capability for example in queue.examples if example.wave == wave} == set(CAPABILITIES)
        for wave in range(1, 21)
    )
    assert {example.split for example in queue.examples if example.wave == 1} == {"development"}
    assert {example.split for example in queue.examples if example.wave == 2} == {"holdout"}
    assert len(queue.queue_hash) == len(queue.development_hash) == len(queue.holdout_hash) == 64
    assert len({example.example_id for example in queue.examples}) == 120


def test_blind_labels_must_cover_complete_balanced_waves_without_prediction_leakage():
    records, claims = candidate_claims()
    queue = build_queue(records, claims, seed="phase-4-frozen")
    valid = [
        {"example_id": example.example_id, "label": "support"}
        for example in queue.examples[:12]
    ]

    labels = validate_labels(queue, valid)

    assert len(labels) == 12
    assert all(label.label is EvidenceLabel.SUPPORT for label in labels)
    assert [label.example_id for label in labels] == [example.example_id for example in queue.examples[:12]]

    invalid_inputs = (
        [*valid, valid[0]],
        valid[:-1],
        [*valid[:4], {"example_id": valid[4]["example_id"], "label": "maybe"}, *valid[5:]],
        [
            *valid[:4],
            {
                "example_id": valid[4]["example_id"],
                "label": "support",
                "system_prediction": "supports",
            },
            *valid[5:],
        ],
    )
    for invalid in invalid_inputs:
        with pytest.raises(ValueError, match="invalid blind labels"):
            validate_labels(queue, invalid)


def test_sealed_label_prefix_can_extend_from_minimum_to_target_but_cannot_change():
    records, claims = candidate_claims()
    queue = build_queue(records, claims, seed="phase-4-frozen")
    submitted = [
        {"example_id": example.example_id, "label": "support"}
        for example in queue.examples
    ]

    extended = validate_label_extension(queue, submitted[:60], submitted)

    assert len(extended) == 120
    changed = [dict(row) for row in submitted]
    changed[0]["label"] = "irrelevant"
    with pytest.raises(ValueError, match="sealed blind labels cannot change"):
        validate_label_extension(queue, submitted[:60], changed)


def test_evidence_gate_requires_blind_human_minimum_and_safe_holdout():
    assert evidence_gate_status(60, human_labels=True, holdout_passed=True) == {
        "passed": True,
        "reason": "60 blind human labels completed and the current holdout safety gate passed",
    }
    assert evidence_gate_status(60, human_labels=False, holdout_passed=True)["passed"] is False
    assert evidence_gate_status(54, human_labels=True, holdout_passed=True)["passed"] is False
    assert evidence_gate_status(60, human_labels=True, holdout_passed=False)["passed"] is False


def unsafe_holdout_fixture() -> tuple[PilotQueue, tuple[FacilityRecord, ...], tuple[BlindLabel, ...]]:
    records: list[FacilityRecord] = []
    examples: list[PilotExample] = []
    labels: list[BlindLabel] = []
    for wave, split in ((1, "development"), (2, "holdout")):
        for capability in CAPABILITIES:
            record_key = f"metric-{capability}-{wave}"
            text = f"{capability} service is listed"
            example_id = f"example-{capability}-{wave}"
            records.append(
                FacilityRecord(
                    record_key=record_key,
                    facility_id=f"facility-{capability}-{wave}",
                    name=f"{capability} metric facility",
                    description=text,
                    capability=(capability,),
                    procedure=("Routine procedure",),
                    equipment=("Generic equipment",),
                    source_urls=(),
                    region="Bihar",
                )
            )
            examples.append(
                PilotExample(
                    example_id=example_id,
                    queue_position=len(examples) + 1,
                    wave=wave,
                    split=split,  # type: ignore[arg-type]
                    record_key=record_key,
                    facility_id=f"facility-{capability}-{wave}",
                    capability=capability,
                    field="description",
                    item_index=0,
                    evidence_text=text,
                    source_urls=(),
                )
            )
            label = (
                EvidenceLabel.IRRELEVANT
                if wave == 2 and capability == "ICU"
                else EvidenceLabel.SUPPORT
            )
            labels.append(BlindLabel(example_id, label))
    return (
        PilotQueue(tuple(examples), "q" * 64, "d" * 64, "h" * 64),
        tuple(records),
        tuple(labels),
    )


def test_metrics_expose_holdout_errors_by_capability_and_check_with_intervals():
    queue, records, labels = unsafe_holdout_fixture()

    summary = evaluate_pilot(
        queue,
        labels,
        records,
        (PresenceCheck(), VocabularyCheck()),
        asserted_claim_count=1_200,
    )

    assert summary["status"] == "in_progress_insufficient_sample"
    assert summary["labels"] == {
        "actual": 12,
        "development": 6,
        "holdout": 6,
        "minimum": 60,
        "target": 120,
    }
    icu_holdout = summary["splits"]["holdout"]["by_capability"]["ICU"]["vocabulary"]
    assert icu_holdout["decisions"] == 1
    assert icu_holdout["abstention_rate"] == 0.0
    assert icu_holdout["correct_decisions"] == 0
    assert icu_holdout["false_support"] == 1
    assert summary["holdout_gate"] == {"passed": False, "unsafe_checks": ["vocabulary"]}
    oncology_development = summary["splits"]["development"]["by_capability"]["oncology"]["vocabulary"]
    assert oncology_development["decision_precision"] == 1.0
    assert oncology_development["precision_95_ci"][0] < 1.0
    assert summary["model_call_projection"]["queue_claims"] == 12
    assert summary["model_call_projection"]["requires_model"] == 12
    assert summary["model_call_projection"]["estimated_live_claims"] == 1_200
    assert summary["model_call_projection"]["economically_plausible"] is False
    assert "accuracy" not in summary


def test_holdout_safety_fallback_makes_generic_sports_injury_language_abstain():
    record = FacilityRecord(
        record_key="sports-rehabilitation",
        facility_id="sports-rehabilitation",
        name="Sports rehabilitation clinic",
        description="Rehabilitation clinic",
        capability=("Provides sports injury rehabilitation",),
        procedure=(),
        equipment=(),
        source_urls=(),
        region="Bihar",
    )
    evidence = ClaimEvidence.from_record(Claim(record.record_key, "trauma"), record)

    result = run_checks(evidence, (VocabularyCheck(),))

    capability_item = next(
        item for item in evidence.items if item.coordinate.field == "capability"
    )
    assert capability_item in result.unresolved
    assert not any(
        decision.coordinate == capability_item.coordinate
        for decision in result.decisions
    )
