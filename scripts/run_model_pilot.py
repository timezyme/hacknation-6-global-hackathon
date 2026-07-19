"""Run the capped Phase 5B Llama modularity and feasibility spike."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any

import mlflow
from databricks.sdk import WorkspaceClient
from run_pilot import _checks, _live_queue, _load_labels, _warehouse_id

from trustdesk.evaluation import BlindLabel, EvidenceLabel, PilotExample, validate_labels
from trustdesk.ladder import CheckAttempt, ClaimEvidence, OutcomeKind, run_checks
from trustdesk.lexicon import CAPABILITIES
from trustdesk.llm_check import (
    DEFAULT_LLAMA_ENDPOINT,
    PARSER_VERSION,
    PROMPT_VERSION,
    DatabricksModelClient,
    LlamaEntailmentCheck,
    ModelClient,
    ModelRateLimitError,
    ModelReply,
    ModelRequest,
    ModelTimeoutError,
)
from trustdesk.marks import Mark
from trustdesk.models import Claim, FacilityRecord

ARTIFACT_PATH = Path("artifacts/model-pilot-summary.json")
PILOT_ARTIFACT_PATH = Path("artifacts/pilot-summary.json")
DEVELOPMENT_CAP = 6
QUALIFICATION_WINDOW_SECONDS = 3_600
MINIMUM_HEADROOM_SAMPLE = 20


@dataclass(frozen=True)
class CallObservation:
    attempt: int
    status: str
    latency_ms: float
    endpoint: str
    model: str | None


@dataclass
class RecordingClient:
    """Record aggregate serving behavior without retaining prompts or model content."""

    wrapped: ModelClient
    observations: list[CallObservation] = field(default_factory=list)

    def classify(self, request: ModelRequest) -> ModelReply:
        started = perf_counter()
        try:
            reply = self.wrapped.classify(request)
        except ModelRateLimitError:
            self.observations.append(
                CallObservation(
                    attempt=request.attempt,
                    status="rate_limit",
                    latency_ms=round((perf_counter() - started) * 1000, 3),
                    endpoint=DEFAULT_LLAMA_ENDPOINT,
                    model=None,
                )
            )
            raise
        except ModelTimeoutError:
            self.observations.append(
                CallObservation(
                    attempt=request.attempt,
                    status="timeout",
                    latency_ms=round((perf_counter() - started) * 1000, 3),
                    endpoint=DEFAULT_LLAMA_ENDPOINT,
                    model=None,
                )
            )
            raise
        self.observations.append(
            CallObservation(
                attempt=request.attempt,
                status="success",
                latency_ms=reply.latency_ms,
                endpoint=reply.endpoint,
                model=reply.model,
            )
        )
        return reply


def _selected_development_examples(
    queue_examples: tuple[PilotExample, ...],
    labels: tuple[BlindLabel, ...],
    records: tuple[FacilityRecord, ...],
) -> tuple[PilotExample, ...]:
    labels_by_id = {label.example_id: label for label in labels}
    records_by_key = {record.record_key: record for record in records}
    free_checks = _checks()
    selected: list[PilotExample] = []
    for capability in CAPABILITIES:
        candidates = (
            example
            for example in queue_examples[: len(labels)]
            if example.split == "development"
            and example.capability == capability
            and example.example_id in labels_by_id
        )
        for example in candidates:
            record = records_by_key[example.record_key]
            evidence = ClaimEvidence.from_record(Claim(example.record_key, capability), record)
            unresolved = {
                item.coordinate
                for item in run_checks(evidence, free_checks).unresolved
            }
            if (example.field, example.item_index) in {
                (coordinate.field, coordinate.item_index) for coordinate in unresolved
            }:
                selected.append(example)
                break
        else:
            raise RuntimeError(f"no development abstention for {capability}")
    if len(selected) != DEVELOPMENT_CAP:
        raise RuntimeError("development cap is not balanced")
    return tuple(selected)


def _predicted_label(attempt: CheckAttempt) -> EvidenceLabel | None:
    if attempt.kind is OutcomeKind.PROCESSING_FAILURE:
        return None
    if attempt.kind is OutcomeKind.ABSTENTION:
        return EvidenceLabel.UNCERTAIN
    if attempt.mark is Mark.SUPPORTS:
        return EvidenceLabel.SUPPORT
    if attempt.mark is Mark.CONFLICTS:
        return EvidenceLabel.REFUTATION
    if attempt.mark is Mark.SILENT:
        return EvidenceLabel.IRRELEVANT
    return None


def _percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(0.95 * len(ordered)) - 1)]


def _pilot_projection() -> dict[str, Any]:
    payload = json.loads(PILOT_ARTIFACT_PATH.read_text())
    projection = payload.get("model_call_projection")
    if not isinstance(projection, dict):
        raise RuntimeError("free-check projection artifact is invalid")
    return projection


def _evaluate(
    profile: str,
    experiment_id: str,
) -> dict[str, Any]:
    os.environ["MLFLOW_TRACKING_URI"] = f"databricks://{profile}"
    os.environ["MLFLOW_TRACING_DESTINATION"] = experiment_id
    mlflow.set_tracking_uri(f"databricks://{profile}")

    workspace = WorkspaceClient(profile=profile)
    warehouse_id = _warehouse_id(workspace)
    queue, records, _ = _live_queue(workspace, warehouse_id)
    labels = validate_labels(queue, _load_labels(workspace, warehouse_id, queue))
    labels_by_id = {label.example_id: label for label in labels}
    records_by_key = {record.record_key: record for record in records}
    selected = _selected_development_examples(queue.examples, labels, records)

    recorder = RecordingClient(
        DatabricksModelClient(
            endpoint=DEFAULT_LLAMA_ENDPOINT,
            serving=workspace.serving_endpoints,
        )
    )
    model_check = LlamaEntailmentCheck(client=recorder)
    free_checks = _checks()
    counts: Counter[str] = Counter()
    for example in selected:
        record = records_by_key[example.record_key]
        evidence = ClaimEvidence.from_record(
            Claim(example.record_key, example.capability),
            record,
        )
        result = run_checks(evidence, (*free_checks, model_check))
        attempt = next(
            item
            for item in result.attempt_history
            if item.check_id == model_check.check_id
            and item.coordinate.field == example.field
            and item.coordinate.item_index == example.item_index
        )
        predicted = _predicted_label(attempt)
        expected = labels_by_id[example.example_id].label
        counts["claims"] += 1
        if attempt.kind is OutcomeKind.PROCESSING_FAILURE:
            counts["processing_failures"] += 1
        elif predicted is expected:
            counts["agreements"] += 1
        else:
            counts["disagreements"] += 1
        if predicted is EvidenceLabel.SUPPORT and expected is not EvidenceLabel.SUPPORT:
            counts["false_support"] += 1
        if predicted is EvidenceLabel.REFUTATION and expected is not EvidenceLabel.REFUTATION:
            counts["false_conflict"] += 1

    successful_latencies = [
        observation.latency_ms
        for observation in recorder.observations
        if observation.status == "success"
    ]
    p95_latency_ms = _percentile_95(successful_latencies)
    projection = _pilot_projection()
    projected_claims = int(projection["estimated_live_claims"])
    observed_attempts_per_claim = (
        len(recorder.observations) / counts["claims"]
        if counts["claims"]
        else 0.0
    )
    projected_calls = ceil(projected_claims * observed_attempts_per_claim)
    projected_serial_seconds = (
        round(projected_calls * p95_latency_ms / 1000, 3)
        if p95_latency_ms is not None
        else None
    )
    quota_failures = sum(
        observation.status == "rate_limit"
        for observation in recorder.observations
    )
    headroom_proven = (
        len(recorder.observations) >= MINIMUM_HEADROOM_SAMPLE
        and quota_failures == 0
    )
    safety_assessed = counts["processing_failures"] < counts["claims"]
    safety_passed = (
        safety_assessed
        and counts["false_support"] == 0
        and counts["false_conflict"] == 0
    )
    structure_passed = counts["processing_failures"] == 0
    throughput_passed = (
        projected_serial_seconds is not None
        and projected_serial_seconds * 2 <= QUALIFICATION_WINDOW_SECONDS
        and headroom_proven
    )
    qualified = safety_passed and structure_passed and throughput_passed

    with mlflow.start_span(
        name="trustdesk.model_pilot.aggregate",
        span_type="CHAIN",
        attributes={
            "trustdesk.endpoint": DEFAULT_LLAMA_ENDPOINT,
            "trustdesk.claims": counts["claims"],
            "trustdesk.false_support": counts["false_support"],
            "trustdesk.false_conflict": counts["false_conflict"],
            "trustdesk.p95_latency_ms": p95_latency_ms or -1,
            "trustdesk.qualified": qualified,
        },
    ):
        pass
    mlflow.flush_trace_async_logging()

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "qualified" if qualified else "disabled_after_capped_spike",
        "selected_mode": "full_model" if qualified else "free_check_only",
        "reference_labels": {
            "kind": "rushed_human_sanity_check",
            "authoritative": False,
            "label_count": len(labels),
        },
        "attempted_model": {
            "family": "Llama",
            "endpoint": DEFAULT_LLAMA_ENDPOINT,
            "prompt_version": PROMPT_VERSION,
            "parser_version": PARSER_VERSION,
        },
        "development": {
            "claims": counts["claims"],
            "capabilities": list(CAPABILITIES),
            "agreements": counts["agreements"],
            "disagreements": counts["disagreements"],
            "false_support": counts["false_support"],
            "false_conflict": counts["false_conflict"],
            "processing_failures": counts["processing_failures"],
        },
        "holdout": {
            "attempted": False,
            "reason": "model disabled before holdout because full-batch throughput and headroom did not qualify",
        },
        "serving": {
            "attempts": len(recorder.observations),
            "successful_calls": len(successful_latencies),
            "quota_failures": quota_failures,
            "timeout_failures": sum(
                observation.status == "timeout"
                for observation in recorder.observations
            ),
            "p95_latency_ms": p95_latency_ms,
            "serial_call_rate_at_p95_per_second": (
                round(1000 / p95_latency_ms, 6)
                if p95_latency_ms
                else None
            ),
            "minimum_headroom_sample": MINIMUM_HEADROOM_SAMPLE,
            "rate_limit_headroom_proven": headroom_proven,
        },
        "projection": {
            "free_check_model_call_rate": projection["model_call_rate"],
            "projected_full_batch_claim_bundles": projected_claims,
            "observed_serving_attempts_per_claim": round(
                observed_attempts_per_claim,
                3,
            ),
            "projected_full_batch_serving_calls": projected_calls,
            "projected_serial_seconds_at_p95": projected_serial_seconds,
            "qualification_window_seconds": QUALIFICATION_WINDOW_SECONDS,
            "can_complete_twice_with_headroom": throughput_passed,
            "currency_cost": None,
            "currency_cost_reason": "endpoint response did not expose a reliable currency price",
        },
        "qualification": {
            "safety_assessed": safety_assessed,
            "safety_passed": safety_passed,
            "structured_output_passed": structure_passed,
            "throughput_and_headroom_passed": throughput_passed,
            "qualified": qualified,
            "disable_reason": None if qualified else "full_batch_not_economically_or_operationally_qualified",
        },
        "qwen_fallback": {
            "attempted": False,
            "reason": "escalation economics fail independently of model choice; no comparative bakeoff",
        },
        "modularity": {
            "runner_changed": False,
            "enable_surface": "one checks.toml entry",
            "default_enabled": False,
        },
        "mlflow": {
            "experiment_path": "/Shared/trustdesk-model-pilot",
            "trace_emission_attempted": True,
            "metadata_readback_verified": False,
            "span_payload_verified": False,
            "verification_limit": "local OAuth refresh prevented span-payload readback",
            "raw_evidence_logging_policy": "code path excludes raw inputs and outputs",
            "live_raw_evidence_absence_verified": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the capped Phase 5B model pilot")
    parser.add_argument("--profile", default="trustdesk-spike")
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    try:
        summary = _evaluate(args.profile, args.experiment_id)
        ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(
            json.dumps(
                {
                    "status": summary["status"],
                    "selected_mode": summary["selected_mode"],
                    "development_claims": summary["development"]["claims"],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(f"model pilot failed ({type(error).__name__})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
