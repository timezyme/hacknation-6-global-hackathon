"""Build complete, deterministic result batches from validated ingest values."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType

from trustdesk.ingest import SOURCE_TABLE
from trustdesk.ladder import (
    Check,
    CheckAttempt,
    CheckRun,
    ClaimEvidence,
    EvidenceCoordinate,
    OutcomeKind,
    run_checks,
)
from trustdesk.marks import FIELDS, Mark, Verdict, derive_verdict, reduce_field
from trustdesk.models import Claim, FacilityRecord, IngestBatch, QuarantinedRecord

PIPELINE_VERSION = "1.0.0"
RefereePayload = Mapping[EvidenceCoordinate, Mapping[str, str]]
RefereeCallback = Callable[[str, tuple[CheckAttempt, ...]], RefereePayload]
_UNREFEREED = MappingProxyType(
    {
        "method": "none",
        "outcome": "could_not_referee",
        "rationale": "No referee was configured for this batch.",
        "version": "none",
    }
)


@dataclass(frozen=True)
class BatchCounts:
    facilities: int
    verdicts: int
    receipts: int
    orphaned_verdicts: int = 0


@dataclass(frozen=True)
class FacilityResult:
    record_key: str
    facility_id: str | None
    facility_name: str | None
    region: str | None
    processing_status: str
    asserted_capabilities: tuple[str, ...]
    quarantine_reasons: tuple[str, ...]


@dataclass(frozen=True)
class VerdictResult:
    record_key: str
    facility_id: str | None
    facility_name: str | None
    region: str | None
    capability: str
    verdict: Verdict
    marks: Mapping[str, Mark | None]
    support_item_count: int
    deciding_checks: tuple[str, ...]
    rank: int | None
    computed_at: datetime


@dataclass(frozen=True)
class ReceiptResult:
    record_key: str
    capability: str | None
    receipt_kind: str
    receipt_json: str
    computed_at: datetime


@dataclass(frozen=True)
class ResultBatch:
    run_id: str
    pipeline_version: str
    input_table: str
    input_table_version: int
    check_config_hash: str
    check_config_json: str
    model_mode: str
    model_version: str | None
    prompt_version: str | None
    computed_at: datetime
    input_count: int
    duplicate_rows_collapsed: int
    facilities: tuple[FacilityResult, ...]
    verdicts: tuple[VerdictResult, ...]
    receipts: tuple[ReceiptResult, ...]

    @property
    def counts(self) -> BatchCounts:
        return BatchCounts(
            facilities=len(self.facilities),
            verdicts=len(self.verdicts),
            receipts=len(self.receipts),
        )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _hash(value: object) -> str:
    return sha256(_json(value).encode()).hexdigest()


def _attempt_payload(attempt: CheckAttempt) -> dict[str, object]:
    return {
        "check_id": attempt.check_id,
        "check_version": attempt.implementation_version,
        "cost_tier": attempt.cost_tier.value,
        "mark": attempt.mark.value if attempt.mark else None,
        "outcome": attempt.kind.value,
        "rationale": attempt.rationale,
        "span": list(attempt.span) if attempt.span else None,
    }


def _field_marks(
    evidence: ClaimEvidence,
    result: CheckRun,
) -> Mapping[str, Mark | None]:
    decisions = {attempt.coordinate: attempt for attempt in result.decisions}
    history: dict[EvidenceCoordinate, list[CheckAttempt]] = defaultdict(list)
    for attempt in result.attempt_history:
        history[attempt.coordinate].append(attempt)

    marks: dict[str, Mark | None] = {}
    for field_name in FIELDS:
        items = tuple(
            item for item in evidence.items if item.coordinate.field == field_name
        )
        item_marks = tuple(
            decision.mark
            for item in items
            if (decision := decisions.get(item.coordinate)) is not None
            and decision.mark is not None
        )
        undecided = tuple(item for item in items if item.coordinate not in decisions)
        processing_failures = sum(
            any(
                attempt.kind is OutcomeKind.PROCESSING_FAILURE
                for attempt in history[item.coordinate]
            )
            for item in undecided
        )
        marks[field_name] = reduce_field(
            item_marks,
            unresolved=len(undecided),
            processing_failures=processing_failures,
        )
    return MappingProxyType(marks)


def _receipt(
    record: FacilityRecord,
    claim: Claim,
    evidence: ClaimEvidence,
    result: CheckRun,
    run_id: str,
    computed_at: datetime,
    referee_findings: RefereePayload,
) -> ReceiptResult:
    decisions = {attempt.coordinate: attempt for attempt in result.decisions}
    history: dict[EvidenceCoordinate, list[CheckAttempt]] = defaultdict(list)
    for attempt in result.attempt_history:
        history[attempt.coordinate].append(attempt)

    items: list[dict[str, object]] = []
    for item in evidence.items:
        decision = decisions.get(item.coordinate)
        attempts = history[item.coordinate]
        failed = any(
            attempt.kind is OutcomeKind.PROCESSING_FAILURE for attempt in attempts
        )
        items.append(
            {
                "attempts": [_attempt_payload(attempt) for attempt in attempts],
                "deciding_check": decision.check_id if decision else None,
                "field": item.coordinate.field,
                "final_outcome": (
                    "decision" if decision else "processing_failure" if failed else "abstention"
                ),
                "item_index": item.coordinate.item_index,
                "mark": decision.mark.value if decision and decision.mark else None,
                "referee": (
                    dict(referee_findings.get(item.coordinate, _UNREFEREED))
                    if decision
                    else None
                ),
                "text": item.text,
            }
        )
    payload = {
        "capability": claim.capability,
        "computed_at": computed_at.isoformat(),
        "facility_id": record.facility_id,
        "items": items,
        "pipeline_run_id": run_id,
        "record_key": record.record_key,
        "source_scope": "row_level_set",
        "source_urls": list(record.source_urls),
    }
    return ReceiptResult(
        record_key=record.record_key,
        capability=claim.capability,
        receipt_kind="claim_evidence",
        receipt_json=_json(payload),
        computed_at=computed_at,
    )


def _accepted_claim(
    record: FacilityRecord,
    claim: Claim,
    checks: Sequence[Check],
    run_id: str,
    computed_at: datetime,
    referee: RefereeCallback | None,
) -> tuple[VerdictResult, ReceiptResult]:
    evidence = ClaimEvidence.from_record(claim, record)
    result = run_checks(evidence, checks)
    referee_findings = (
        referee(claim.capability, result.decisions) if referee is not None else {}
    )
    decision_coordinates = {decision.coordinate for decision in result.decisions}
    if not set(referee_findings).issubset(decision_coordinates):
        raise ValueError("referee returned a finding for an undecided item")
    for finding in referee_findings.values():
        if (
            set(finding) != {"method", "outcome", "rationale", "version"}
            or finding["outcome"]
            not in {"agree", "disagree", "could_not_referee"}
            or not all(isinstance(value, str) and value for value in finding.values())
        ):
            raise ValueError("referee returned an invalid finding")
    marks = _field_marks(evidence, result)
    verdict = VerdictResult(
        record_key=record.record_key,
        facility_id=record.facility_id,
        facility_name=record.name,
        region=record.region,
        capability=claim.capability,
        verdict=derive_verdict(tuple(marks[field_name] for field_name in FIELDS)),
        marks=marks,
        support_item_count=sum(
            attempt.mark is Mark.SUPPORTS for attempt in result.decisions
        ),
        deciding_checks=tuple(
            dict.fromkeys(attempt.check_id for attempt in result.decisions)
        ),
        rank=None,
        computed_at=computed_at,
    )
    return verdict, _receipt(
        record,
        claim,
        evidence,
        result,
        run_id,
        computed_at,
        referee_findings,
    )


def _quarantine_receipt(
    record: QuarantinedRecord,
    capability: str | None,
    run_id: str,
    computed_at: datetime,
) -> ReceiptResult:
    payload = {
        "capability": capability,
        "computed_at": computed_at.isoformat(),
        "facility_id": record.facility_id,
        "pipeline_run_id": run_id,
        "quarantine_reasons": list(record.reasons),
        "record_key": record.record_key,
    }
    return ReceiptResult(
        record_key=record.record_key,
        capability=capability,
        receipt_kind="quarantine",
        receipt_json=_json(payload),
        computed_at=computed_at,
    )


def _rank(rows: Sequence[VerdictResult]) -> tuple[VerdictResult, ...]:
    ranked: dict[tuple[str, str | None], list[VerdictResult]] = defaultdict(list)
    output: list[VerdictResult] = []
    for row in rows:
        if row.verdict in (Verdict.STRONG_SUPPORT, Verdict.LIMITED_SUPPORT):
            ranked[(row.capability, row.region)].append(row)
        else:
            output.append(row)
    for group in ranked.values():
        ordered = sorted(
            group,
            key=lambda row: (
                0 if row.verdict is Verdict.STRONG_SUPPORT else 1,
                -row.support_item_count,
                (row.facility_name or "").casefold(),
                row.record_key,
            ),
        )
        output.extend(
            replace(row, rank=index) for index, row in enumerate(ordered, start=1)
        )
    return tuple(sorted(output, key=lambda row: (row.capability, row.record_key)))


def build_result_batch(
    ingest: IngestBatch,
    checks: Sequence[Check],
    *,
    input_table_version: int,
    computed_at: datetime | None = None,
    input_table: str = SOURCE_TABLE,
    pipeline_version: str = PIPELINE_VERSION,
    referee: RefereeCallback | None = None,
    referee_config: Mapping[str, object] | None = None,
) -> ResultBatch:
    """Build all rows for one immutable source/configuration snapshot."""
    computed_at = computed_at or datetime.now(UTC)
    records = {record.record_key: record for record in ingest.records}
    quarantined = {record.record_key: record for record in ingest.quarantined}
    if len(records) != len(ingest.records) or len(quarantined) != len(ingest.quarantined):
        raise ValueError("duplicate record key")
    if set(records) & set(quarantined):
        raise ValueError("duplicate record key")
    if len(set(ingest.claims)) != len(ingest.claims):
        raise ValueError("duplicate claim")

    check_config = tuple(
        {
            "check_id": check.check_id,
            "cost_tier": check.cost_tier.value,
            "implementation_version": check.implementation_version,
        }
        for check in checks
    )
    pipeline_config = {
        "checks": check_config,
        "referee": referee_config
        or {"enabled": False, "mode": "none", "version": "none"},
    }
    check_config_json = _json(pipeline_config)
    check_config_hash = _hash(pipeline_config)
    run_id = _hash(
        {
            "check_config_hash": check_config_hash,
            "input_table": input_table,
            "input_table_version": input_table_version,
            "model_mode": "disabled",
            "pipeline_version": pipeline_version,
        }
    )

    facilities = tuple(
        FacilityResult(
            record_key=record.record_key,
            facility_id=record.facility_id,
            facility_name=record.name,
            region=record.region,
            processing_status="accepted",
            asserted_capabilities=tuple(
                claim.capability
                for claim in ingest.claims
                if claim.record_key == record.record_key
            ),
            quarantine_reasons=(),
        )
        for record in ingest.records
    ) + tuple(
        FacilityResult(
            record_key=record.record_key,
            facility_id=record.facility_id,
            facility_name=None,
            region=record.region,
            processing_status="quarantined",
            asserted_capabilities=record.asserted_capabilities,
            quarantine_reasons=record.reasons,
        )
        for record in ingest.quarantined
    )

    verdicts: list[VerdictResult] = []
    receipts: list[ReceiptResult] = []
    for claim in ingest.claims:
        if claim.record_key in records:
            verdict, receipt = _accepted_claim(
                records[claim.record_key],
                claim,
                checks,
                run_id,
                computed_at,
                referee,
            )
        elif claim.record_key in quarantined:
            record = quarantined[claim.record_key]
            failed_marks = MappingProxyType(
                {field_name: Mark.FAILED for field_name in FIELDS}
            )
            verdict = VerdictResult(
                record_key=record.record_key,
                facility_id=record.facility_id,
                facility_name=None,
                region=record.region,
                capability=claim.capability,
                verdict=derive_verdict(
                    tuple(failed_marks[field_name] for field_name in FIELDS),
                    quarantined=True,
                ),
                marks=failed_marks,
                support_item_count=0,
                deciding_checks=(),
                rank=None,
                computed_at=computed_at,
            )
            receipt = _quarantine_receipt(
                record, claim.capability, run_id, computed_at
            )
        else:
            raise ValueError("claim references unknown record key")
        verdicts.append(verdict)
        receipts.append(receipt)

    claimed_quarantines = {claim.record_key for claim in ingest.claims}
    receipts.extend(
        _quarantine_receipt(record, None, run_id, computed_at)
        for record in ingest.quarantined
        if record.record_key not in claimed_quarantines
    )
    return ResultBatch(
        run_id=run_id,
        pipeline_version=pipeline_version,
        input_table=input_table,
        input_table_version=input_table_version,
        check_config_hash=check_config_hash,
        check_config_json=check_config_json,
        model_mode="disabled",
        model_version=None,
        prompt_version=None,
        computed_at=computed_at,
        input_count=ingest.input_count,
        duplicate_rows_collapsed=ingest.duplicate_rows_collapsed,
        facilities=tuple(sorted(facilities, key=lambda row: row.record_key)),
        verdicts=_rank(verdicts),
        receipts=tuple(
            sorted(
                receipts,
                key=lambda row: (row.record_key, row.capability or ""),
            )
        ),
    )
