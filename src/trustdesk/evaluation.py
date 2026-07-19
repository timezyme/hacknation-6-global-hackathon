"""Deterministic queueing and aggregate evaluation for the blind free-check pilot."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from math import sqrt
from typing import Any, Literal

from trustdesk.ladder import (
    Check,
    ClaimEvidence,
    EvidenceCoordinate,
    EvidenceItem,
    OutcomeKind,
    run_checks,
)
from trustdesk.lexicon import CAPABILITIES, CAPABILITY_TERMS, REFUTATION_PATTERNS
from trustdesk.marks import Mark
from trustdesk.models import Claim, FacilityRecord

Split = Literal["development", "holdout"]
SPLIT_PATTERN: tuple[Split, ...] = (
    "development",
    "holdout",
    "development",
    "development",
    "holdout",
) * 4
MINIMUM_LABELS = 60
TARGET_LABELS = 120
MAX_MODEL_CALL_RATE = 0.5
GENERIC_NEGATIVE = re.compile(
    r"\b(?:no|not|without|referred|closed|unavailable|ceased|discontinued)\b",
    re.IGNORECASE,
)


class EvidenceLabel(StrEnum):
    """Blind reviewer reading of one selected evidence item."""

    SUPPORT = "support"
    REFUTATION = "refutation"
    IRRELEVANT = "irrelevant"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class PilotExample:
    """One asserted claim plus one blind evidence item selected without running checks."""

    example_id: str
    queue_position: int
    wave: int
    split: Split
    record_key: str
    facility_id: str
    capability: str
    field: str
    item_index: int
    evidence_text: str | None
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class PilotQueue:
    """Frozen manifests for a balanced, wave-ordered pilot queue."""

    examples: tuple[PilotExample, ...]
    queue_hash: str
    development_hash: str
    holdout_hash: str


@dataclass(frozen=True)
class BlindLabel:
    """Validated label with no system output attached."""

    example_id: str
    label: EvidenceLabel


def _hash(payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(serialized.encode()).hexdigest()


def _manifest_hash(examples: Sequence[PilotExample]) -> str:
    return _hash(
        [
            {
                "example_id": example.example_id,
                "queue_position": example.queue_position,
                "split": example.split,
            }
            for example in examples
        ]
    )


def _example(
    record: FacilityRecord,
    claim: Claim,
    *,
    seed: str,
    ordinal: int,
    queue_position: int,
) -> PilotExample:
    evidence = ClaimEvidence.from_record(claim, record)
    evidence_hash = _hash([seed, "evidence", claim.record_key, claim.capability])
    item = evidence.items[int(evidence_hash, 16) % len(evidence.items)]
    example_id = _hash(
        [
            claim.record_key,
            claim.capability,
            item.coordinate.field,
            item.coordinate.item_index,
            item.text,
        ]
    )
    return PilotExample(
        example_id=example_id,
        queue_position=queue_position,
        wave=ordinal + 1,
        split=SPLIT_PATTERN[ordinal],
        record_key=record.record_key,
        facility_id=record.facility_id,
        capability=claim.capability,
        field=item.coordinate.field,
        item_index=item.coordinate.item_index,
        evidence_text=item.text,
        source_urls=record.source_urls,
    )


def build_queue(
    records: Sequence[FacilityRecord],
    claims: Sequence[Claim],
    *,
    seed: str,
    claims_per_capability: int = 20,
) -> PilotQueue:
    """Freeze a representative queue without consulting any system prediction."""
    if claims_per_capability != len(SPLIT_PATTERN):
        raise ValueError("the Phase 4 queue requires exactly 20 claims per capability")
    records_by_key = {record.record_key: record for record in records}
    claims_by_capability: dict[str, dict[str, Claim]] = defaultdict(dict)
    for claim in claims:
        if claim.record_key in records_by_key and claim.capability in CAPABILITIES:
            claims_by_capability[claim.capability][claim.record_key] = claim

    selected: dict[str, tuple[Claim, ...]] = {}
    for capability in CAPABILITIES:
        ordered = sorted(
            claims_by_capability[capability].values(),
            key=lambda claim: (_hash([seed, claim.record_key, claim.capability]), claim.record_key),
        )
        if len(ordered) < claims_per_capability:
            raise ValueError(f"not enough claims for {capability}")
        selected[capability] = tuple(ordered[:claims_per_capability])

    examples: list[PilotExample] = []
    for ordinal in range(claims_per_capability):
        for capability in CAPABILITIES:
            claim = selected[capability][ordinal]
            examples.append(
                _example(
                    records_by_key[claim.record_key],
                    claim,
                    seed=seed,
                    ordinal=ordinal,
                    queue_position=len(examples) + 1,
                )
            )

    frozen = tuple(examples)
    development = tuple(example for example in frozen if example.split == "development")
    holdout = tuple(example for example in frozen if example.split == "holdout")
    return PilotQueue(
        examples=frozen,
        queue_hash=_manifest_hash(frozen),
        development_hash=_manifest_hash(development),
        holdout_hash=_manifest_hash(holdout),
    )


def validate_labels(
    queue: PilotQueue,
    rows: Sequence[Mapping[str, object]],
) -> tuple[BlindLabel, ...]:
    """Accept only a contiguous prefix of complete six-capability waves."""
    try:
        if len(rows) > len(queue.examples) or len(rows) % len(CAPABILITIES) != 0:
            raise ValueError
        parsed: dict[str, BlindLabel] = {}
        for row in rows:
            if set(row) != {"example_id", "label"}:
                raise ValueError
            example_id = row["example_id"]
            label = row["label"]
            if not isinstance(example_id, str) or not isinstance(label, str) or example_id in parsed:
                raise ValueError
            parsed[example_id] = BlindLabel(example_id, EvidenceLabel(label))

        expected = tuple(example.example_id for example in queue.examples[: len(rows)])
        if set(parsed) != set(expected):
            raise ValueError
        return tuple(parsed[example_id] for example_id in expected)
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid blind labels") from None


def validate_label_extension(
    queue: PilotQueue,
    sealed_rows: Sequence[Mapping[str, object]],
    submitted_rows: Sequence[Mapping[str, object]],
) -> tuple[BlindLabel, ...]:
    """Allow more complete waves while preserving every already sealed label."""
    sealed = validate_labels(queue, sealed_rows)
    submitted = validate_labels(queue, submitted_rows)
    if submitted[: len(sealed)] != sealed:
        raise ValueError("sealed blind labels cannot change")
    return submitted


def _wilson(successes: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.96
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    spread = z * sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, centre - spread), 6), round(min(1.0, centre + spread), 6)]


def rule_configuration_hash(checks: Sequence[Check]) -> str:
    """Hash executable check order, versions, vocabulary, and refutation terms."""
    return _hash(
        {
            "checks": [
                {
                    "check_id": check.check_id,
                    "implementation_version": check.implementation_version,
                    "cost_tier": check.cost_tier.value,
                }
                for check in checks
            ],
            "capability_terms": CAPABILITY_TERMS,
            "refutation_patterns": REFUTATION_PATTERNS,
        }
    )


def _prediction(mark: Mark | None) -> EvidenceLabel | None:
    if mark is Mark.SUPPORTS:
        return EvidenceLabel.SUPPORT
    if mark is Mark.CONFLICTS:
        return EvidenceLabel.REFUTATION
    if mark in {Mark.SILENT, Mark.MISSING}:
        return EvidenceLabel.IRRELEVANT
    return None


def _metric(
    sample_count: int,
    attempts: int,
    decisions: int,
    abstentions: int,
    processing_failures: int,
    correct: int,
    false_support: int,
    false_conflict: int,
) -> dict[str, object]:
    return {
        "sample_count": sample_count,
        "attempts": attempts,
        "decisions": decisions,
        "abstentions": abstentions,
        "abstention_rate": round(abstentions / attempts, 6) if attempts else None,
        "processing_failures": processing_failures,
        "selective_coverage": round(decisions / sample_count, 6) if sample_count else None,
        "coverage_95_ci": _wilson(decisions, sample_count),
        "correct_decisions": correct,
        "errors": decisions - correct,
        "decision_precision": round(correct / decisions, 6) if decisions else None,
        "precision_95_ci": _wilson(correct, decisions),
        "false_support": false_support,
        "false_conflict": false_conflict,
    }


def evaluate_pilot(
    queue: PilotQueue,
    labels: Sequence[BlindLabel],
    records: Sequence[FacilityRecord],
    checks: Sequence[Check],
    *,
    asserted_claim_count: int,
) -> dict[str, Any]:
    """Score blind evidence labels and project claim-level model demand."""
    if asserted_claim_count < 0 or not checks:
        raise ValueError("invalid pilot inputs")
    examples_by_id = {example.example_id: example for example in queue.examples}
    records_by_key = {record.record_key: record for record in records}
    label_ids = tuple(label.example_id for label in labels)
    expected_ids = tuple(example.example_id for example in queue.examples[: len(labels)])
    if len(set(label_ids)) != len(label_ids) or set(label_ids) != set(expected_ids):
        raise ValueError("invalid pilot inputs")

    sample_counts: defaultdict[tuple[str, str], int] = defaultdict(int)
    counters: defaultdict[tuple[str, str, str, str], int] = defaultdict(int)
    labels_by_id = {label.example_id: label for label in labels}
    for example in queue.examples[: len(labels)]:
        label = labels_by_id[example.example_id]
        sample_counts[(example.split, example.capability)] += 1
        evidence = ClaimEvidence(
            claim=Claim(example.record_key, example.capability),
            items=(
                EvidenceItem(
                    EvidenceCoordinate(example.field, example.item_index),
                    example.evidence_text,
                ),
            ),
            source_urls=example.source_urls,
        )
        result = run_checks(evidence, checks)
        for attempt in result.attempt_history:
            key = (example.split, example.capability, attempt.check_id)
            counters[(*key, "attempts")] += 1
            if attempt.kind is OutcomeKind.ABSTENTION:
                counters[(*key, "abstentions")] += 1
            elif attempt.kind is OutcomeKind.PROCESSING_FAILURE:
                counters[(*key, "processing_failures")] += 1
            elif attempt.kind is OutcomeKind.DECISION:
                counters[(*key, "decisions")] += 1
                predicted = _prediction(attempt.mark)
                if predicted is label.label:
                    counters[(*key, "correct")] += 1
                elif predicted is EvidenceLabel.SUPPORT:
                    counters[(*key, "false_support")] += 1
                elif predicted is EvidenceLabel.REFUTATION:
                    counters[(*key, "false_conflict")] += 1

    split_payload: dict[str, object] = {}
    for split in ("development", "holdout"):
        by_capability: dict[str, object] = {}
        for capability in CAPABILITIES:
            by_check: dict[str, object] = {}
            sample_count = sample_counts[(split, capability)]
            for check in checks:
                key = (split, capability, check.check_id)
                by_check[check.check_id] = _metric(
                    sample_count,
                    counters[(*key, "attempts")],
                    counters[(*key, "decisions")],
                    counters[(*key, "abstentions")],
                    counters[(*key, "processing_failures")],
                    counters[(*key, "correct")],
                    counters[(*key, "false_support")],
                    counters[(*key, "false_conflict")],
                )
            by_capability[capability] = by_check
        split_payload[split] = {
            "denominator": sum(sample_counts[(split, capability)] for capability in CAPABILITIES),
            "by_capability": by_capability,
        }

    unsafe_checks = sorted(
        check.check_id
        for check in checks
        if any(
            counters[("holdout", capability, check.check_id, "false_support")]
            + counters[("holdout", capability, check.check_id, "false_conflict")]
            > 0
            for capability in CAPABILITIES
        )
    )

    queued_by_capability: defaultdict[str, int] = defaultdict(int)
    model_by_capability: defaultdict[str, int] = defaultdict(int)
    for example in queue.examples:
        record = records_by_key.get(example.record_key)
        if record is None:
            raise ValueError("invalid pilot inputs")
        queued_by_capability[example.capability] += 1
        full_evidence = ClaimEvidence.from_record(
            Claim(example.record_key, example.capability),
            record,
        )
        if run_checks(full_evidence, checks).unresolved:
            model_by_capability[example.capability] += 1
    queue_claims = len(queue.examples)
    requires_model = sum(model_by_capability.values())
    model_rate = requires_model / queue_claims if queue_claims else 0.0

    development_count = sum(
        examples_by_id[label.example_id].split == "development"
        for label in labels
    )
    holdout_count = len(labels) - development_count
    target_refutations = sum(label.label is EvidenceLabel.REFUTATION for label in labels)
    generic_negative_hits = sum(
        bool(GENERIC_NEGATIVE.search(examples_by_id[label.example_id].evidence_text or ""))
        for label in labels
    )
    return {
        "schema_version": 1,
        "status": (
            "complete_preliminary" if len(labels) >= MINIMUM_LABELS else "in_progress_insufficient_sample"
        ),
        "labels": {
            "actual": len(labels),
            "development": development_count,
            "holdout": holdout_count,
            "minimum": MINIMUM_LABELS,
            "target": TARGET_LABELS,
        },
        "hashes": {
            "queue": queue.queue_hash,
            "development_manifest": queue.development_hash,
            "holdout_manifest": queue.holdout_hash,
            "rule_configuration": rule_configuration_hash(checks),
        },
        "splits": split_payload,
        "holdout_gate": {"passed": not unsafe_checks, "unsafe_checks": unsafe_checks},
        "contradiction_prevalence": {
            "labelled_items": len(labels),
            "target_bound_refutations": target_refutations,
            "generic_negative_language_hits": generic_negative_hits,
        },
        "model_call_projection": {
            "queue_claims": queue_claims,
            "free_settled": queue_claims - requires_model,
            "requires_model": requires_model,
            "model_call_rate": round(model_rate, 6),
            "estimated_live_claims": round(asserted_claim_count * model_rate),
            "live_asserted_claims": asserted_claim_count,
            "economically_plausible": model_rate <= MAX_MODEL_CALL_RATE,
            "maximum_plausible_rate": MAX_MODEL_CALL_RATE,
            "by_capability": {
                capability: {
                    "queue_claims": queued_by_capability[capability],
                    "requires_model": model_by_capability[capability],
                }
                for capability in CAPABILITIES
            },
        },
    }
