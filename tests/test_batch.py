"""Complete-batch construction and publication behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from trustdesk.batch import BatchCounts, ResultBatch, build_result_batch
from trustdesk.ingest import ingest_rows
from trustdesk.ladder import (
    CheckAttempt,
    CheckFinding,
    ClaimEvidence,
    CostTier,
    EvidenceCoordinate,
    OutcomeKind,
)
from trustdesk.marks import Mark, Verdict
from trustdesk.models import Claim, FacilityRecord, IngestBatch
from trustdesk.sink import (
    PublicationStatus,
    decode_receipt,
    encode_receipt,
    publish_batch,
)

NOW = datetime(2026, 7, 19, tzinfo=UTC)


@dataclass(frozen=True)
class ScriptedCheck:
    check_id: str = "scripted"
    implementation_version: str = "1.0.0"
    cost_tier: CostTier = CostTier.FREE
    fail: bool = False

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        if self.fail:
            raise TimeoutError("synthetic failure")
        findings: list[CheckFinding] = []
        for item in evidence.items:
            text = item.text or ""
            if text == "support":
                findings.append(
                    CheckFinding(
                        OutcomeKind.DECISION,
                        item.coordinate,
                        Mark.SUPPORTS,
                        "Explicit support.",
                    )
                )
            elif text == "conflict":
                findings.append(
                    CheckFinding(
                        OutcomeKind.DECISION,
                        item.coordinate,
                        Mark.CONFLICTS,
                        "Explicit conflict.",
                    )
                )
            elif item.text is None:
                findings.append(
                    CheckFinding(
                        OutcomeKind.DECISION,
                        item.coordinate,
                        Mark.MISSING,
                        "Field is blank.",
                    )
                )
            else:
                findings.append(
                    CheckFinding(
                        OutcomeKind.ABSTENTION,
                        item.coordinate,
                        None,
                        "Cannot decide safely.",
                    )
                )
        return tuple(findings)


def record(
    key: str,
    *,
    description: str = "support",
    capability: tuple[str, ...] = ("support",),
    equipment: tuple[str, ...] = ("support",),
    procedure: tuple[str, ...] = ("support",),
) -> FacilityRecord:
    return FacilityRecord(
        record_key=key,
        facility_id=f"facility-{key}",
        name=f"Facility {key}",
        description=description,
        capability=capability,
        equipment=equipment,
        procedure=procedure,
        source_urls=(f"https://example.test/{key}",),
        region="Bihar",
    )


def accepted_batch(facility: FacilityRecord) -> IngestBatch:
    return IngestBatch(
        input_count=1,
        accepted_input_count=1,
        quarantined_input_count=0,
        records=(facility,),
        claims=(Claim(facility.record_key, "ICU"),),
        quarantined=(),
        duplicate_rows_collapsed=0,
    )


def build(facility: FacilityRecord, check: ScriptedCheck | None = None) -> ResultBatch:
    return build_result_batch(
        accepted_batch(facility),
        (check or ScriptedCheck(),),
        input_table_version=7,
        computed_at=NOW,
    )


def test_complete_batch_reduces_marks_and_keeps_full_attempt_receipts():
    batch = build(record("mixed", equipment=("conflict",)))

    assert batch.counts == BatchCounts(facilities=1, verdicts=1, receipts=1)
    verdict = batch.verdicts[0]
    assert verdict.verdict is Verdict.CONFLICTING
    assert verdict.marks["equipment"] is Mark.CONFLICTS
    receipt = json.loads(batch.receipts[0].receipt_json)
    assert receipt["record_key"] == "mixed"
    assert receipt["pipeline_run_id"] == batch.run_id
    assert receipt["source_urls"] == ["https://example.test/mixed"]
    assert receipt["items"][2]["field"] == "equipment"
    assert receipt["items"][2]["attempts"][0]["outcome"] == "decision"
    assert receipt["items"][2]["referee"] == {
        "method": "none",
        "outcome": "could_not_referee",
        "rationale": "No referee was configured for this batch.",
        "version": "none",
    }
    assert decode_receipt(encode_receipt(batch.receipts[0].receipt_json)) == (
        batch.receipts[0].receipt_json
    )


def test_configured_referee_fields_are_receipt_data_and_change_run_identity():
    facility = record("refereed")
    baseline = build(facility)

    def referee(
        capability: str,
        decisions: tuple[CheckAttempt, ...],
    ) -> dict[EvidenceCoordinate, dict[str, str]]:
        assert capability == "ICU"
        return {
            decision.coordinate: {
                "method": "independent_test",
                "outcome": "agree",
                "rationale": "Independent test agrees.",
                "version": "test-1",
            }
            for decision in decisions
        }

    batch = build_result_batch(
        accepted_batch(facility),
        (ScriptedCheck(),),
        input_table_version=7,
        computed_at=NOW,
        referee=referee,
        referee_config={"enabled": True, "mode": "rules", "version": "test-1"},
    )
    receipt = json.loads(batch.receipts[0].receipt_json)

    assert batch.run_id != baseline.run_id
    assert batch.verdicts == baseline.verdicts
    assert {
        item["referee"]["outcome"]
        for item in receipt["items"]
        if item["final_outcome"] == "decision"
    } == {"agree"}


def test_all_abstain_and_processing_failure_remain_distinguishable_in_receipts():
    uncertain = build(
        record(
            "uncertain",
            description="unknown",
            capability=("unknown",),
            equipment=("unknown",),
            procedure=("unknown",),
        )
    )
    failed = build(record("failed"), ScriptedCheck(fail=True))

    assert uncertain.verdicts[0].verdict is Verdict.NOT_ENOUGH_DATA
    assert set(uncertain.verdicts[0].marks.values()) == {None}
    assert failed.verdicts[0].verdict is Verdict.COULD_NOT_CHECK
    uncertain_receipt = json.loads(uncertain.receipts[0].receipt_json)
    failed_receipt = json.loads(failed.receipts[0].receipt_json)
    assert {item["final_outcome"] for item in uncertain_receipt["items"]} == {"abstention"}
    assert {item["final_outcome"] for item in failed_receipt["items"]} == {
        "processing_failure"
    }


def test_quarantine_is_indexed_and_recoverable_claim_becomes_could_not_check():
    raw = {
        "unique_id": "facility-1",
        "name": "Broken ICU listing",
        "description": "Hospital",
        "capability": '["ICU"]',
        "equipment": "not-json",
        "procedure": "[]",
        "source_urls": "[]",
        "address_stateOrRegion": "Bihar",
    }
    batch = build_result_batch(
        ingest_rows((raw,)),
        (ScriptedCheck(),),
        input_table_version=7,
        computed_at=NOW,
    )

    assert batch.facilities[0].processing_status == "quarantined"
    assert batch.verdicts[0].verdict is Verdict.COULD_NOT_CHECK
    assert batch.receipts[0].receipt_kind == "quarantine"
    assert "invalid_array" in batch.receipts[0].receipt_json


def test_duplicate_record_delivery_is_rejected_before_publication():
    facility = record("duplicate")
    duplicate = replace(accepted_batch(facility), records=(facility, facility))

    with pytest.raises(ValueError, match="duplicate record key"):
        build_result_batch(
            duplicate,
            (ScriptedCheck(),),
            input_table_version=7,
            computed_at=NOW,
        )


@dataclass
class MemorySink:
    fail_after_facilities: bool = False
    active: str | None = None
    statuses: dict[str, str] = field(default_factory=dict)
    facilities: dict[tuple[str, str], object] = field(default_factory=dict)
    verdicts: dict[tuple[str, str, str], object] = field(default_factory=dict)
    receipts: dict[tuple[str, str, str | None], object] = field(default_factory=dict)

    def is_complete(self, run_id: str) -> bool:
        return self.statuses.get(run_id) == "complete"

    def active_run_id(self) -> str | None:
        return self.active

    def begin(self, batch: ResultBatch) -> None:
        self.statuses[batch.run_id] = "writing"
        self.facilities = {
            key: value for key, value in self.facilities.items() if key[0] != batch.run_id
        }
        self.verdicts = {
            key: value for key, value in self.verdicts.items() if key[0] != batch.run_id
        }
        self.receipts = {
            key: value for key, value in self.receipts.items() if key[0] != batch.run_id
        }

    def write(self, batch: ResultBatch) -> None:
        for row in batch.facilities:
            self.facilities[(batch.run_id, row.record_key)] = row
        if self.fail_after_facilities:
            raise RuntimeError("partial write")
        for row in batch.verdicts:
            self.verdicts[(batch.run_id, row.record_key, row.capability)] = row
        for row in batch.receipts:
            self.receipts[(batch.run_id, row.record_key, row.capability)] = row

    def counts(self, run_id: str) -> BatchCounts:
        return BatchCounts(
            facilities=sum(key[0] == run_id for key in self.facilities),
            verdicts=sum(key[0] == run_id for key in self.verdicts),
            receipts=sum(key[0] == run_id for key in self.receipts),
        )

    def complete(self, batch: ResultBatch, actual: BatchCounts) -> None:
        assert actual == batch.counts
        self.statuses[batch.run_id] = "complete"

    def fail(self, run_id: str) -> None:
        self.statuses[run_id] = "failed"

    def activate(self, run_id: str, published_at: datetime) -> None:
        assert self.statuses[run_id] == "complete"
        assert published_at.tzinfo is UTC
        self.active = run_id


def test_partial_write_never_replaces_active_run_and_retry_is_idempotent():
    first = build(record("first"))
    second = build_result_batch(
        accepted_batch(record("second")),
        (ScriptedCheck(implementation_version="1.1.0"),),
        input_table_version=7,
        computed_at=NOW,
    )
    sink = MemorySink()

    assert publish_batch(sink, first) is PublicationStatus.PUBLISHED
    sink.fail_after_facilities = True
    with pytest.raises(RuntimeError, match="partial write"):
        publish_batch(sink, second)
    assert sink.active == first.run_id
    assert sink.statuses[first.run_id] == "complete"

    sink.fail_after_facilities = False
    assert publish_batch(sink, second) is PublicationStatus.PUBLISHED
    counts_after_retry = sink.counts(second.run_id)
    assert publish_batch(sink, second) is PublicationStatus.ALREADY_COMPLETE
    assert sink.counts(second.run_id) == counts_after_retry == second.counts
    assert sink.active == second.run_id
    assert sink.statuses[first.run_id] == "complete"


def test_completed_run_retry_repairs_a_pointer_left_on_the_previous_run():
    batch = build(record("completed"))
    sink = MemorySink()
    assert publish_batch(sink, batch) is PublicationStatus.PUBLISHED
    sink.active = "previous-run"

    assert publish_batch(sink, batch) is PublicationStatus.ALREADY_COMPLETE
    assert sink.active == batch.run_id
    assert sink.counts(batch.run_id) == batch.counts


def test_similar_context_attaches_to_ranked_claims_only() -> None:
    """Amendment unit 7: comparison context is fetched batch-time for ranked verdicts."""
    import json

    from trustdesk.batch import build_result_batch
    from trustdesk.check_presence import PresenceCheck
    from trustdesk.check_vocabulary import VocabularyCheck
    from trustdesk.ingest import ingest_rows

    requested: list[tuple[str, str]] = []

    def similar(record, capability):  # matches SimilarCallback at runtime
        requested.append((record.record_key, capability))
        return {"framing": "comparison only", "neighbors": [{"facility_id": "fac-n", "score": 0.9}]}

    rows = (
        {
            "unique_id": "vf-ranked",
            "name": "Ranked Hospital",
            "description": "Full intensive care unit with ventilator support on site.",
            "capability": '["ICU", "intensive care"]',
            "procedure": '["intensive care admission"]',
            "equipment": '["ventilator"]',
            "source_urls": '["https://example.org"]',
            "address_stateOrRegion": "Kerala",
            "address_country": "India",
            "address_countryCode": "IN",
            "_row_fingerprint": "f1",
        },
        {
            "unique_id": "vf-sparse",
            "name": "Sparse Hospital",
            "description": None,
            "capability": '["ICU"]',
            "procedure": None,
            "equipment": None,
            "source_urls": None,
            "address_stateOrRegion": "Kerala",
            "address_country": "India",
            "address_countryCode": "IN",
            "_row_fingerprint": "f2",
        },
    )
    batch = build_result_batch(
        ingest_rows(rows),
        (PresenceCheck(), VocabularyCheck()),
        input_table_version=1,
        similar=similar,
        similar_config={"enabled": True, "scope": "ranked_claims_only", "version": "1.0.0"},
    )
    ranked = {v.record_key: v for v in batch.verdicts if v.verdict.value in ("strong_support", "limited_support")}
    unranked = [v for v in batch.verdicts if v.verdict.value not in ("strong_support", "limited_support")]
    assert ranked and unranked
    assert {key for key, _ in requested} == set(ranked)
    payloads = {r.record_key: json.loads(r.receipt_json) for r in batch.receipts}
    for key in ranked:
        assert payloads[key]["similar"]["neighbors"][0]["facility_id"] == "fac-n"
    for verdict in unranked:
        assert "similar" not in payloads[verdict.record_key]


def test_similar_config_changes_the_run_id() -> None:
    from trustdesk.batch import build_result_batch
    from trustdesk.check_presence import PresenceCheck
    from trustdesk.ingest import ingest_rows

    rows = (
        {
            "unique_id": "vf-1",
            "name": "Hospital",
            "description": "ICU",
            "capability": '["ICU"]',
            "procedure": None,
            "equipment": None,
            "source_urls": None,
            "address_stateOrRegion": "Kerala",
            "address_country": "India",
            "address_countryCode": "IN",
            "_row_fingerprint": "f1",
        },
    )
    base = build_result_batch(ingest_rows(rows), (PresenceCheck(),), input_table_version=1)
    enabled = build_result_batch(
        ingest_rows(rows),
        (PresenceCheck(),),
        input_table_version=1,
        similar_config={"enabled": True, "scope": "ranked_claims_only", "version": "1.0.0"},
    )
    assert base.run_id != enabled.run_id
