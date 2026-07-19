"""Build and publish the bounded live-data slice used by the walking-skeleton demo."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem

from trustdesk.ladder import Check, CheckAttempt, ClaimEvidence, EvidenceCoordinate, run_checks
from trustdesk.lexicon import CAPABILITIES
from trustdesk.marks import FIELDS, Mark
from trustdesk.models import Claim, FacilityRecord

RESULTS_TABLE = "workspace.default.trustdesk_walking_skeleton"
RESULT_COLUMNS = (
    "run_id",
    "run_status",
    "published_at",
    "record_key",
    "facility_id",
    "facility_name",
    "region",
    "capability",
    "rank",
    "support_tier",
    "support_field_count",
    "support_item_count",
    "unresolved_item_count",
    "marks_json",
    "receipt_json",
    "source_urls_json",
    "unknown_summary",
)
CREATE_RESULTS_TABLE = f"""CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
    run_id STRING NOT NULL,
    run_status STRING NOT NULL,
    published_at TIMESTAMP NOT NULL,
    record_key STRING NOT NULL,
    facility_id STRING NOT NULL,
    facility_name STRING NOT NULL,
    region STRING NOT NULL,
    capability STRING NOT NULL,
    rank INT NOT NULL,
    support_tier STRING NOT NULL,
    support_field_count INT NOT NULL,
    support_item_count INT NOT NULL,
    unresolved_item_count INT NOT NULL,
    marks_json STRING NOT NULL,
    receipt_json STRING NOT NULL,
    source_urls_json STRING NOT NULL,
    unknown_summary STRING NOT NULL
) USING DELTA"""


@dataclass(frozen=True)
class SkeletonRow:
    """One selected facility claim and its receipt-ready free-check result."""

    run_id: str
    run_status: str
    published_at: datetime
    record_key: str
    facility_id: str
    facility_name: str
    region: str
    capability: str
    rank: int
    support_tier: str
    support_field_count: int
    support_item_count: int
    unresolved_item_count: int
    marks_json: str
    receipt_json: str
    source_urls_json: str
    unknown_summary: str


@dataclass(frozen=True)
class SkeletonSlice:
    """A complete versioned slice ready for one atomic Delta insert."""

    run_id: str
    published_at: datetime
    selection_hash: str
    rows: tuple[SkeletonRow, ...]
    model_requests: int = 0


def _row_values(row: SkeletonRow) -> tuple[object, ...]:
    return (
        row.run_id,
        row.run_status,
        row.published_at,
        row.record_key,
        row.facility_id,
        row.facility_name,
        row.region,
        row.capability,
        row.rank,
        row.support_tier,
        row.support_field_count,
        row.support_item_count,
        row.unresolved_item_count,
        row.marks_json,
        row.receipt_json,
        row.source_urls_json,
        row.unknown_summary,
    )


def _parameter(name: str, value: object) -> StatementParameterListItem:
    if isinstance(value, datetime):
        return StatementParameterListItem(name=name, type="TIMESTAMP", value=value.isoformat())
    if isinstance(value, int):
        return StatementParameterListItem(name=name, type="INT", value=str(value))
    return StatementParameterListItem(name=name, type="STRING", value=str(value))


def _execute(
    workspace: WorkspaceClient,
    warehouse_id: str,
    statement: str,
    parameters: Sequence[StatementParameterListItem] = (),
) -> None:
    response = workspace.statement_execution.execute_statement(
        statement,
        warehouse_id,
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
        parameters=list(parameters),
        wait_timeout="50s",
    )
    state = getattr(response.status, "state", None)
    if state is None or state.value != "SUCCEEDED":
        raise RuntimeError("walking-skeleton publish statement failed")


def publish_slice(workspace: WorkspaceClient, warehouse_id: str, batch: SkeletonSlice) -> None:
    """Create the target once, then atomically insert one complete versioned run."""
    if not batch.rows or any(row.run_status != "complete" for row in batch.rows):
        raise ValueError("only a non-empty completed slice can be published")
    _execute(workspace, warehouse_id, CREATE_RESULTS_TABLE)

    parameters: list[StatementParameterListItem] = []
    value_groups: list[str] = []
    for row_index, row in enumerate(batch.rows):
        names = tuple(f"{column}_{row_index}" for column in RESULT_COLUMNS)
        value_groups.append("(" + ",".join(f":{name}" for name in names) + ")")
        parameters.extend(
            _parameter(name, value)
            for name, value in zip(names, _row_values(row), strict=True)
        )
    statement = (
        f"INSERT INTO {RESULTS_TABLE} ({','.join(RESULT_COLUMNS)}) VALUES "
        + ",".join(value_groups)
    )
    _execute(workspace, warehouse_id, statement, parameters)


def _attempt_payload(attempt: CheckAttempt) -> dict[str, object]:
    return {
        "outcome": attempt.kind.value,
        "check_id": attempt.check_id,
        "check_version": attempt.implementation_version,
        "cost_tier": attempt.cost_tier.value,
        "rationale": attempt.rationale,
    }


def _receipt(evidence: ClaimEvidence, decisions: Sequence[CheckAttempt], history: Sequence[CheckAttempt]) -> str:
    decisions_by_coordinate = {attempt.coordinate: attempt for attempt in decisions}
    history_by_coordinate: dict[EvidenceCoordinate, list[CheckAttempt]] = defaultdict(list)
    for attempt in history:
        history_by_coordinate[attempt.coordinate].append(attempt)

    items: list[dict[str, object]] = []
    for item in evidence.items:
        decision = decisions_by_coordinate.get(item.coordinate)
        attempts = history_by_coordinate[item.coordinate]
        items.append(
            {
                "field": item.coordinate.field,
                "item_index": item.coordinate.item_index,
                "text": item.text,
                "mark": decision.mark.value if decision and decision.mark else "unresolved",
                "outcome": decision.kind.value if decision else "abstention",
                "deciding_check": decision.check_id if decision else None,
                "check_version": decision.implementation_version if decision else None,
                "rationale": decision.rationale if decision else attempts[-1].rationale,
                "attempts": [_attempt_payload(attempt) for attempt in attempts],
            }
        )
    return json.dumps(items, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _field_marks(evidence: ClaimEvidence, decisions: Sequence[CheckAttempt]) -> dict[str, str]:
    decisions_by_coordinate = {attempt.coordinate: attempt for attempt in decisions}
    marks: dict[str, str] = {}
    for field in FIELDS:
        items = tuple(item for item in evidence.items if item.coordinate.field == field)
        item_marks = tuple(
            decision.mark
            for item in items
            if (decision := decisions_by_coordinate.get(item.coordinate)) is not None
        )
        if Mark.SUPPORTS in item_marks:
            marks[field] = Mark.SUPPORTS.value
        elif item_marks and len(item_marks) == len(items) and all(mark is Mark.MISSING for mark in item_marks):
            marks[field] = Mark.MISSING.value
        else:
            marks[field] = "unresolved"
    return marks


def _row(
    record: FacilityRecord,
    claim: Claim,
    checks: Sequence[Check],
    run_id: str,
    published_at: datetime,
) -> SkeletonRow:
    evidence = ClaimEvidence.from_record(claim, record)
    result = run_checks(evidence, checks)
    marks = _field_marks(evidence, result.decisions)
    support_fields = sum(mark == Mark.SUPPORTS.value for mark in marks.values())
    support_items = sum(attempt.mark is Mark.SUPPORTS for attempt in result.decisions)
    unresolved_items = len(result.unresolved)
    unknown = (
        f"{unresolved_items} evidence item(s) remain unresolved. " if unresolved_items else ""
    ) + "Current capability, staffing, and equipment condition are not independently verified."
    return SkeletonRow(
        run_id=run_id,
        run_status="complete",
        published_at=published_at,
        record_key=record.record_key,
        facility_id=record.facility_id,
        facility_name=record.name or "Unnamed facility",
        region=record.region or "Unknown region",
        capability=claim.capability,
        rank=0,
        support_tier="strong_support" if support_fields >= 3 else "limited_support",
        support_field_count=support_fields,
        support_item_count=support_items,
        unresolved_item_count=unresolved_items,
        marks_json=json.dumps(marks, separators=(",", ":"), sort_keys=True),
        receipt_json=_receipt(evidence, result.decisions, result.attempt_history),
        source_urls_json=json.dumps(record.source_urls, separators=(",", ":")),
        unknown_summary=unknown,
    )


def _select(candidates: Sequence[SkeletonRow], count: int) -> tuple[SkeletonRow, ...]:
    ordered = sorted(
        candidates,
        key=lambda row: (
            -row.support_field_count,
            row.unresolved_item_count,
            -row.support_item_count,
            row.facility_name.casefold(),
            row.record_key,
        ),
    )
    if len(ordered) < count:
        raise ValueError("not enough valid candidates for walking-skeleton slice")
    selected = [ordered[0]]
    second_region = next((row for row in ordered[1:] if row.region != selected[0].region), None)
    if count > 1 and second_region is None:
        raise ValueError("walking-skeleton candidates require at least two regions")
    if second_region is not None:
        selected.append(second_region)
    selected.extend(row for row in ordered if row not in selected)
    return tuple(selected[:count])


def build_slice(
    records: Sequence[FacilityRecord],
    claims: Sequence[Claim],
    checks: Sequence[Check],
    *,
    candidates_per_capability: int = 5,
    run_id: str,
    published_at: datetime | None = None,
) -> SkeletonSlice:
    """Select the strongest reproducible candidates while forcing regional diversity."""
    published_at = published_at or datetime.now(UTC)
    records_by_key = {record.record_key: record for record in records}
    candidates: dict[str, list[SkeletonRow]] = defaultdict(list)
    for claim in claims:
        record = records_by_key.get(claim.record_key)
        if record is not None and record.region is not None:
            candidates[claim.capability].append(_row(record, claim, checks, run_id, published_at))

    selected: list[SkeletonRow] = []
    for capability in CAPABILITIES:
        selected.extend(_select(candidates[capability], candidates_per_capability))

    ranked: list[SkeletonRow] = []
    for capability in CAPABILITIES:
        capability_rows = [row for row in selected if row.capability == capability]
        for region in sorted({row.region for row in capability_rows}):
            region_rows = sorted(
                (row for row in capability_rows if row.region == region),
                key=lambda row: (
                    0 if row.support_tier == "strong_support" else 1,
                    -row.support_item_count,
                    row.facility_name.casefold(),
                    row.record_key,
                ),
            )
            ranked.extend(replace(row, rank=index) for index, row in enumerate(region_rows, start=1))

    hash_payload = {
        "checks": [(check.check_id, check.implementation_version) for check in checks],
        "rows": [(row.record_key, row.capability) for row in ranked],
    }
    selection_hash = sha256(
        json.dumps(hash_payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return SkeletonSlice(
        run_id=run_id,
        published_at=published_at,
        selection_hash=selection_hash,
        rows=tuple(ranked),
    )
