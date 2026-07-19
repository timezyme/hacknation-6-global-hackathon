"""Prepare, label, and report the Phase 4 blind free-check pilot."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    Disposition,
    Format,
    StatementParameterListItem,
    StatementResponse,
)

from trustdesk.evaluation import (
    PilotQueue,
    build_queue,
    evaluate_pilot,
    sanity_check_status,
    validate_label_extension,
    validate_labels,
)
from trustdesk.ingest import ingest_rows, load_live_rows
from trustdesk.ladder import Check, load_checks
from trustdesk.lexicon import CAPABILITIES
from trustdesk.models import FacilityRecord

PROFILE = "trustdesk-spike"
QUEUE_SEED = "trustdesk-phase-4-v1"
LABEL_SET = "human-v1"
SANITY_CHECK_LABELER = "human_reviewer"
LABEL_TABLE = "workspace.default.trustdesk_pilot_labels"
ARTIFACT_PATH = Path("artifacts/pilot-summary.json")
REPORT_PATH = Path("docs/pilot-results.md")
HOLDOUT_SAFETY_ACTION = {
    "check_id": "vocabulary",
    "capability": "trauma",
    "case_class": "generic injury or injuries terms without explicit trauma context",
    "failing_example_id": "501d65158a3853492eec94633b7d8089dc06813ab0c8e8d84cd9fba826b75e9e",
    "initial_false_supports": 1,
    "action": "removed the generic terms; the vocabulary check now abstains",
}
CREATE_LABEL_TABLE = f"""CREATE TABLE IF NOT EXISTS {LABEL_TABLE} (
    pilot_id STRING NOT NULL,
    queue_hash STRING NOT NULL,
    development_hash STRING NOT NULL,
    holdout_hash STRING NOT NULL,
    queue_position INT NOT NULL,
    wave INT NOT NULL,
    split STRING NOT NULL,
    example_id STRING NOT NULL,
    record_key STRING NOT NULL,
    facility_id STRING NOT NULL,
    capability STRING NOT NULL,
    field STRING NOT NULL,
    item_index INT NOT NULL,
    evidence_text STRING,
    source_urls_json STRING NOT NULL,
    label STRING,
    labeler STRING,
    labeled_at TIMESTAMP
) USING DELTA"""
QUEUE_COLUMNS = (
    "pilot_id",
    "queue_hash",
    "development_hash",
    "holdout_hash",
    "queue_position",
    "wave",
    "split",
    "example_id",
    "record_key",
    "facility_id",
    "capability",
    "field",
    "item_index",
    "evidence_text",
    "source_urls_json",
)


def _warehouse_id(workspace: WorkspaceClient) -> str:
    warehouses = tuple(workspace.warehouses.list())
    if len(warehouses) != 1 or not warehouses[0].id:
        raise RuntimeError("expected exactly one SQL warehouse")
    return warehouses[0].id


def _rows(response: StatementResponse) -> tuple[dict[str, Any], ...]:
    state = getattr(response.status, "state", None)
    if state is None or state.value != "SUCCEEDED":
        raise RuntimeError("pilot SQL statement failed")
    if response.manifest is None or response.manifest.schema is None or response.result is None:
        return ()
    schema_columns = response.manifest.schema.columns or []
    columns = tuple(column.name for column in schema_columns if column.name is not None)
    if len(columns) != len(schema_columns):
        raise RuntimeError("pilot SQL returned an unnamed column")
    return tuple(
        dict(zip(columns, values, strict=True))
        for values in response.result.data_array or []
    )


def _execute(
    workspace: WorkspaceClient,
    warehouse_id: str,
    statement: str,
    parameters: Sequence[StatementParameterListItem] = (),
) -> tuple[dict[str, Any], ...]:
    response = workspace.statement_execution.execute_statement(
        statement,
        warehouse_id,
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
        parameters=list(parameters),
        row_limit=10_000,
        wait_timeout="50s",
    )
    return _rows(response)


def _parameter(name: str, value: object) -> StatementParameterListItem:
    if value is None:
        return StatementParameterListItem(name=name, type="STRING", value=None)
    if isinstance(value, int):
        return StatementParameterListItem(name=name, type="INT", value=str(value))
    if isinstance(value, datetime):
        return StatementParameterListItem(name=name, type="TIMESTAMP", value=value.isoformat())
    return StatementParameterListItem(name=name, type="STRING", value=str(value))


def _live_queue(
    workspace: WorkspaceClient,
    warehouse_id: str,
) -> tuple[PilotQueue, tuple[FacilityRecord, ...], int]:
    batch = ingest_rows(load_live_rows(workspace, warehouse_id))
    queue = build_queue(batch.records, batch.claims, seed=QUEUE_SEED)
    return queue, batch.records, len(batch.claims)


def _pilot_id(queue: PilotQueue) -> str:
    return f"pilot-{queue.queue_hash[:16]}-{LABEL_SET}"


def _queue_values(queue: PilotQueue) -> tuple[tuple[object, ...], ...]:
    pilot_id = _pilot_id(queue)
    return tuple(
        (
            pilot_id,
            queue.queue_hash,
            queue.development_hash,
            queue.holdout_hash,
            example.queue_position,
            example.wave,
            example.split,
            example.example_id,
            example.record_key,
            example.facility_id,
            example.capability,
            example.field,
            example.item_index,
            example.evidence_text,
            json.dumps(example.source_urls, separators=(",", ":")),
        )
        for example in queue.examples
    )


def prepare_queue(
    workspace: WorkspaceClient,
    warehouse_id: str,
    queue: PilotQueue,
) -> None:
    """Create an idempotent label queue containing no system predictions."""
    _execute(workspace, warehouse_id, CREATE_LABEL_TABLE)
    existing = _execute(
        workspace,
        warehouse_id,
        f"""SELECT COUNT(*) AS row_count, COUNT(DISTINCT queue_hash) AS queue_hashes
            FROM {LABEL_TABLE} WHERE pilot_id = :pilot_id""",
        (StatementParameterListItem(name="pilot_id", value=_pilot_id(queue)),),
    )
    if existing and int(existing[0]["row_count"]) > 0:
        if int(existing[0]["row_count"]) != 120 or int(existing[0]["queue_hashes"]) != 1:
            raise RuntimeError("stored pilot queue is incomplete")
        return

    parameters: list[StatementParameterListItem] = []
    groups: list[str] = []
    for row_index, values in enumerate(_queue_values(queue)):
        names = tuple(f"{column}_{row_index}" for column in QUEUE_COLUMNS)
        groups.append("(" + ",".join(f":{name}" for name in names) + ")")
        parameters.extend(
            _parameter(name, value)
            for name, value in zip(names, values, strict=True)
        )
    statement = (
        f"INSERT INTO {LABEL_TABLE} ({','.join(QUEUE_COLUMNS)}) VALUES "
        + ",".join(groups)
    )
    _execute(workspace, warehouse_id, statement, parameters)


def export_blind_items(
    workspace: WorkspaceClient,
    warehouse_id: str,
    queue: PilotQueue,
    output: Path,
    waves: int,
) -> None:
    """Export only evidence and queue metadata; refuse to place raw rows in the repository."""
    if not 1 <= waves <= 20:
        raise ValueError("waves must be between 1 and 20")
    if output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("blind evidence export must stay outside the repository")
    rows = _execute(
        workspace,
        warehouse_id,
        f"""SELECT example_id, queue_position, wave, split, capability, field,
                   item_index, evidence_text
            FROM {LABEL_TABLE}
            WHERE pilot_id = :pilot_id AND wave <= :waves
            ORDER BY queue_position""",
        (
            StatementParameterListItem(name="pilot_id", value=_pilot_id(queue)),
            StatementParameterListItem(name="waves", type="INT", value=str(waves)),
        ),
    )
    if len(rows) != waves * len(CAPABILITIES):
        raise RuntimeError("blind evidence export is incomplete")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")


def _label_payload(path: Path) -> tuple[Mapping[str, object], ...]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError("label file must contain a JSON list")
    return tuple(payload)


def apply_labels(
    workspace: WorkspaceClient,
    warehouse_id: str,
    queue: PilotQueue,
    path: Path,
    labeler: str,
) -> int:
    """Seal a contiguous set of complete balanced waves; existing labels cannot change."""
    submitted = _label_payload(path)
    labels = validate_labels(queue, submitted)
    if not labeler.strip() or len(labeler) > 100:
        raise ValueError("labeler name is invalid")
    existing = _execute(
        workspace,
        warehouse_id,
        f"""SELECT example_id, label FROM {LABEL_TABLE}
            WHERE pilot_id = :pilot_id AND label IS NOT NULL ORDER BY queue_position""",
        (StatementParameterListItem(name="pilot_id", value=_pilot_id(queue)),),
    )
    if existing:
        try:
            labels = validate_label_extension(queue, existing, submitted)
        except ValueError as error:
            raise RuntimeError(str(error)) from None
        if len(existing) == len(labels):
            return len(labels)

    parameters: list[StatementParameterListItem] = [
        StatementParameterListItem(name="pilot_id", value=_pilot_id(queue)),
        StatementParameterListItem(name="labeler", value=labeler.strip()),
        StatementParameterListItem(
            name="labeled_at",
            type="TIMESTAMP",
            value=datetime.now(UTC).isoformat(),
        ),
    ]
    groups: list[str] = []
    for index, label in enumerate(labels):
        example_name = f"example_id_{index}"
        label_name = f"label_{index}"
        groups.append(f"(:{example_name},:{label_name})")
        parameters.extend(
            (
                StatementParameterListItem(name=example_name, value=label.example_id),
                StatementParameterListItem(name=label_name, value=label.label.value),
            )
        )
    _execute(
        workspace,
        warehouse_id,
        f"""MERGE INTO {LABEL_TABLE} AS target
            USING (SELECT * FROM VALUES {','.join(groups)} AS source(example_id, label))
            ON target.pilot_id = :pilot_id AND target.example_id = source.example_id
            WHEN MATCHED AND target.label IS NULL THEN UPDATE SET
              target.label = source.label,
              target.labeler = :labeler,
              target.labeled_at = :labeled_at""",
        parameters,
    )
    return len(labels)


def _load_labels(
    workspace: WorkspaceClient,
    warehouse_id: str,
    queue: PilotQueue,
) -> tuple[Mapping[str, object], ...]:
    return _execute(
        workspace,
        warehouse_id,
        f"""SELECT example_id, label FROM {LABEL_TABLE}
            WHERE pilot_id = :pilot_id AND label IS NOT NULL ORDER BY queue_position""",
        (StatementParameterListItem(name="pilot_id", value=_pilot_id(queue)),),
    )


def _load_labelers(
    workspace: WorkspaceClient,
    warehouse_id: str,
    queue: PilotQueue,
) -> tuple[str, ...]:
    rows = _execute(
        workspace,
        warehouse_id,
        f"""SELECT DISTINCT labeler FROM {LABEL_TABLE}
            WHERE pilot_id = :pilot_id AND label IS NOT NULL ORDER BY labeler""",
        (StatementParameterListItem(name="pilot_id", value=_pilot_id(queue)),),
    )
    labelers: list[str] = []
    for row in rows:
        labeler = row.get("labeler")
        if not isinstance(labeler, str) or not labeler:
            raise RuntimeError("sealed labels have invalid reviewer provenance")
        labelers.append(labeler)
    if not labelers:
        raise RuntimeError("sealed labels have invalid reviewer provenance")
    return tuple(labelers)


def _percent(value: object) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{value * 100:.1f}%"


def _interval(value: object) -> str:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(bound, (int, float)) for bound in value)
    ):
        return "—"
    lower, upper = value
    assert isinstance(lower, (int, float)) and isinstance(upper, (int, float))
    return f"{lower * 100:.1f} to {upper * 100:.1f}%"


def render_report(summary: Mapping[str, Any]) -> str:
    """Render the rushed sanity check without implying authoritative accuracy."""
    labels = summary["labels"]
    model = summary["model_call_projection"]
    gate = summary["holdout_gate"]
    sanity_check = summary["sanity_check"]
    check_label = "COMPLETE" if sanity_check["completed"] else "INCOMPLETE"
    if not sanity_check["completed"] or not gate["passed"]:
        decision = "NO-GO: an accepted free check made a false support or conflict on holdout."
    elif not model["economically_plausible"]:
        decision = "MOVE ON with the free-check demo; do not scale the model path at this escalation rate."
    else:
        decision = "MOVE ON: the safety fallback is verified and projected model demand is within the threshold."

    lines = [
        "# Free-check sanity check",
        "",
        f"**Status:** `{summary['status']}`",
        "",
        f"**Sanity check:** {check_label} — {sanity_check['reason']}.",
        "",
        f"**Decision:** {decision}",
        "",
        (
            f"{labels['actual']} evidence items were labelled in complete six-capability waves "
            f"({labels['development']} development, {labels['holdout']} holdout; minimum {labels['minimum']})."
        ),
        "A rushed human reviewer saw each selected row item without the system prediction.",
        "This sanity check is not authoritative, ground truth, final accuracy, or proof of current capability.",
        "",
        "## Holdout safety action",
        "",
        (
            "The first sealed holdout score caught one false trauma support from the generic "
            "`injury` case class. The terms `injury` and `injuries` were removed, so that class now abstains."
        ),
        "This was the only holdout-driven change and it narrowed the rule to abstention; no support term was added.",
        "The initial failure is manually recorded history; current metrics and reviewer provenance are recomputed.",
        "",
        "## Model-call economics",
        "",
        (
            f"The free checks fully settled {model['free_settled']} of {model['queue_claims']} queued claims. "
            f"{model['requires_model']} ({_percent(model['model_call_rate'])}) would require one model bundle call."
        ),
        (
            f"At the same rate, approximately {model['estimated_live_claims']} of "
            f"{model['live_asserted_claims']} live asserted claims would escalate."
        ),
        "",
        "## Split results",
        "",
    ]
    for split in ("development", "holdout"):
        payload = summary["splits"][split]
        lines.extend((f"### {split.title()} (n={payload['denominator']})", ""))
        lines.append(
            "| Capability | Check | Coverage (95% CI) | Abstention rate | Precision (95% CI) | Errors |"
        )
        lines.append("|---|---|---:|---:|---:|---:|")
        for capability in CAPABILITIES:
            for check_id, metric in payload["by_capability"][capability].items():
                lines.append(
                    f"| {capability} | {check_id} | {_percent(metric['selective_coverage'])} "
                    f"({_interval(metric['coverage_95_ci'])}) | {_percent(metric['abstention_rate'])} | "
                    f"{_percent(metric['decision_precision'])} "
                    f"({_interval(metric['precision_95_ci'])}) | {metric['errors']} |"
                )
        lines.append("")

    contradiction = summary["contradiction_prevalence"]
    hashes = summary["hashes"]
    lines.extend(
        (
            "## Contradiction prevalence",
            "",
            (
                f"Target-bound refutation labels: {contradiction['target_bound_refutations']} of "
                f"{contradiction['labelled_items']}. Generic negative-language hits: "
                f"{contradiction['generic_negative_language_hits']}."
            ),
            "These counts stay separate because generic negatives often describe unrelated services or boilerplate.",
            "",
            "## Frozen manifests",
            "",
            f"- Pilot: `{summary['pilot_id']}`",
            f"- Queue: `{hashes['queue']}`",
            f"- Development: `{hashes['development_manifest']}`",
            f"- Holdout: `{hashes['holdout_manifest']}`",
            f"- Rule configuration: `{hashes['rule_configuration']}`",
            "",
            "This preliminary pilot is not equivalent to the audit's proposed 300-claim experiment.",
        )
    )
    return "\n".join(lines) + "\n"


def write_results(summary: dict[str, Any]) -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    REPORT_PATH.write_text(render_report(summary))


def _checks() -> tuple[Check, ...]:
    return load_checks(Path("config/checks.toml"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 4 blind pilot")
    parser.add_argument("command", choices=("prepare", "export", "apply-labels", "report"))
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--waves", type=int, default=10)
    parser.add_argument("--labeler", default=SANITY_CHECK_LABELER)
    args = parser.parse_args()
    stage = args.command
    try:
        workspace = WorkspaceClient(profile=args.profile)
        warehouse_id = _warehouse_id(workspace)
        queue, records, asserted_claim_count = _live_queue(workspace, warehouse_id)
        if args.command == "prepare":
            prepare_queue(workspace, warehouse_id, queue)
            result = {"status": "pass", "queued_claims": len(queue.examples)}
        elif args.command == "export":
            if args.output is None:
                raise ValueError("--output is required")
            export_blind_items(workspace, warehouse_id, queue, args.output, args.waves)
            result = {"status": "pass", "exported_claims": args.waves * len(CAPABILITIES)}
        elif args.command == "apply-labels":
            if args.input is None:
                raise ValueError("--input is required")
            count = apply_labels(workspace, warehouse_id, queue, args.input, args.labeler)
            result = {"status": "pass", "sealed_labels": count}
        else:
            labels = validate_labels(queue, _load_labels(workspace, warehouse_id, queue))
            labelers = _load_labelers(workspace, warehouse_id, queue)
            summary = evaluate_pilot(
                queue,
                labels,
                records,
                _checks(),
                asserted_claim_count=asserted_claim_count,
            )
            summary["generated_at"] = datetime.now(UTC).isoformat()
            summary["pilot_id"] = _pilot_id(queue)
            review_kind = (
                "rushed_human_sanity_check"
                if labelers == (SANITY_CHECK_LABELER,)
                else "non_authoritative_rehearsal"
            )
            summary["labeling"] = {
                "blind": True,
                "reviewers": list(labelers),
                "reviewer_type": labelers[0] if len(labelers) == 1 else "multiple_reviewers",
                "review_kind": review_kind,
                "authoritative": False,
                "unit": "one deterministically sampled evidence item per queued claim",
            }
            summary["sanity_check"] = sanity_check_status(
                summary["labels"]["actual"],
                holdout_passed=summary["holdout_gate"]["passed"],
            )
            summary["status"] = (
                "sanity_check_complete"
                if summary["sanity_check"]["completed"]
                else "sanity_check_incomplete"
            )
            summary["holdout_gate"]["initial_observed_failures"] = {
                "false_support": 1,
                "false_conflict": 0,
            }
            summary["holdout_gate"]["history_provenance"] = (
                "manually recorded from the first sealed holdout score before the abstention fallback"
            )
            summary["holdout_gate"]["safety_actions"] = [HOLDOUT_SAFETY_ACTION]
            write_results(summary)
            result = {
                "status": "pass",
                "labels": summary["labels"]["actual"],
                "pilot_status": summary["status"],
            }
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        print(f"pilot {stage}: fail ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
