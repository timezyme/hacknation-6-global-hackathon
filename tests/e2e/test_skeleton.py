"""End-to-end contract for the bounded walking-skeleton demo."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trustdesk.check_presence import PresenceCheck
from trustdesk.check_vocabulary import VocabularyCheck
from trustdesk.models import Claim, FacilityRecord
from trustdesk.skeleton_batch import RESULTS_TABLE, build_slice, publish_slice

sys.path.insert(0, str(Path(__file__).parents[2]))
app_module = import_module("app.main")
FacilityData = app_module.FacilityData
ReviewRecord = app_module.ReviewRecord
create_app = app_module.create_app

TARGET_TEXT = {
    "ICU": "Intensive care unit with ventilator support",
    "maternity": "Maternity service with a labour room and delivery care",
    "emergency": "Emergency service with resuscitation",
    "oncology": "Oncology service offering chemotherapy",
    "trauma": "Trauma centre providing fracture care",
    "NICU": "NICU with neonatal incubator support",
}


class MemoryResults:
    def __init__(self, facilities: tuple[FacilityData, ...]) -> None:
        self.facilities = facilities

    def options(self) -> dict[str, object]:
        return {
            "run_id": "run-live",
            "capabilities": ["ICU", "maternity", "emergency", "oncology", "trauma", "NICU"],
            "regions": ["Bihar", "Odisha"],
            "model_requests": 0,
        }

    def search(self, capability: str, region: str) -> tuple[FacilityData, ...]:
        return tuple(
            facility
            for facility in self.facilities
            if facility.capability == capability and facility.region == region
        )

    def receipt(self, record_key: str, capability: str) -> FacilityData | None:
        return next(
            (
                facility
                for facility in self.facilities
                if facility.record_key == record_key and facility.capability == capability
            ),
            None,
        )


class MemoryReviews:
    def __init__(self) -> None:
        self.saved: list[ReviewRecord] = []

    def save(self, review: ReviewRecord) -> ReviewRecord:
        self.saved.append(review)
        return review

    def latest(self, record_key: str, capability: str, reviewer: str) -> ReviewRecord | None:
        return next(
            (
                review
                for review in reversed(self.saved)
                if review.record_key == record_key
                and review.capability == capability
                and review.reviewer == reviewer
            ),
            None,
        )


async def asgi_request(
    app: Any,
    method: str,
    path: str,
    *,
    query: str = "",
    body: dict[str, object] | None = None,
    headers: tuple[tuple[str, str], ...] = (),
) -> tuple[int, dict[str, Any]]:
    encoded_body = json.dumps(body).encode() if body is not None else b""
    encoded_headers = [(name.lower().encode(), value.encode()) for name, value in headers]
    if body is not None:
        encoded_headers.append((b"content-type", b"application/json"))
    messages = [{"type": "http.request", "body": encoded_body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": encoded_headers,
        "client": ("test", 123),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, json.loads(response_body or b"{}")


def candidate_records() -> tuple[tuple[FacilityRecord, ...], tuple[Claim, ...]]:
    records: list[FacilityRecord] = []
    claims: list[Claim] = []
    for capability, text in TARGET_TEXT.items():
        for index, region in enumerate(("Bihar", "Odisha"), start=1):
            record_key = f"record-{capability}-{index}"
            records.append(
                FacilityRecord(
                    record_key=record_key,
                    facility_id=f"facility-{capability}-{index}",
                    name=f"{capability} Candidate {index}",
                    description=text,
                    capability=(text,),
                    equipment=(text,),
                    procedure=(text,),
                    source_urls=(f"https://example.test/{capability}/{index}",),
                    region=region,
                )
            )
            claims.append(Claim(record_key, capability))
    return tuple(records), tuple(claims)


def test_reproducible_slice_covers_every_capability_and_exposes_exact_receipts():
    records, claims = candidate_records()
    noisy = FacilityRecord(
        record_key="record-ICU-noisy",
        facility_id="facility-ICU-noisy",
        name="AAA Noisy ICU Candidate",
        description=TARGET_TEXT["ICU"],
        capability=(TARGET_TEXT["ICU"], *("Generic service entry",) * 20),
        equipment=(TARGET_TEXT["ICU"],),
        procedure=(TARGET_TEXT["ICU"],),
        source_urls=("https://example.test/ICU/noisy",),
        region="Bihar",
    )

    result = build_slice(
        (noisy, *records),
        (Claim(noisy.record_key, "ICU"), *claims),
        (PresenceCheck(), VocabularyCheck()),
        candidates_per_capability=2,
        run_id="run-test",
        published_at=datetime(2026, 7, 19, tzinfo=UTC),
    )

    assert len(result.rows) == 12
    assert {row.capability for row in result.rows} == set(TARGET_TEXT)
    assert all(
        {row.region for row in result.rows if row.capability == capability} == {"Bihar", "Odisha"}
        for capability in TARGET_TEXT
    )
    assert all(row.run_id == "run-test" and row.run_status == "complete" for row in result.rows)
    assert all(row.support_tier == "strong_support" for row in result.rows)
    assert all(row.rank == 1 for row in result.rows)
    assert noisy.record_key not in {row.record_key for row in result.rows}
    assert len(result.selection_hash) == 64
    assert result.model_requests == 0

    first = result.rows[0]
    receipt = json.loads(first.receipt_json)
    sources = json.loads(first.source_urls_json)
    assert receipt[0]["field"] == "description"
    assert receipt[0]["item_index"] == 0
    assert receipt[0]["text"] == TARGET_TEXT[first.capability]
    assert receipt[0]["outcome"] == "decision"
    assert receipt[0]["deciding_check"] == "vocabulary"
    assert receipt[0]["check_version"] == "1.0.0"
    assert sources == [f"https://example.test/{first.capability}/1"]
    assert "not independently verified" in first.unknown_summary


def test_delta_publish_is_one_parameterized_insert_for_the_complete_slice():
    records, claims = candidate_records()
    result = build_slice(
        records,
        claims,
        (PresenceCheck(), VocabularyCheck()),
        candidates_per_capability=2,
        run_id="run-test",
        published_at=datetime(2026, 7, 19, tzinfo=UTC),
    )

    class Statements:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        def execute_statement(self, statement: str, warehouse_id: str, **kwargs: Any) -> Any:
            self.calls.append((statement, warehouse_id, kwargs))
            return SimpleNamespace(status=SimpleNamespace(state=SimpleNamespace(value="SUCCEEDED")))

    statements = Statements()
    workspace = SimpleNamespace(statement_execution=statements)

    publish_slice(workspace, "warehouse-1", result)

    assert len(statements.calls) == 2
    create_sql, create_warehouse, create_options = statements.calls[0]
    insert_sql, insert_warehouse, insert_options = statements.calls[1]
    assert create_sql.startswith(f"CREATE TABLE IF NOT EXISTS {RESULTS_TABLE}")
    assert create_warehouse == insert_warehouse == "warehouse-1"
    assert create_options["parameters"] == []
    assert insert_sql.startswith(f"INSERT INTO {RESULTS_TABLE}")
    assert insert_sql.count("(:run_id_") == len(result.rows)
    assert "DELETE" not in insert_sql
    assert len(insert_options["parameters"]) == len(result.rows) * 17
    assert len({parameter.name for parameter in insert_options["parameters"]}) == len(
        insert_options["parameters"]
    )


def test_app_completes_rank_receipt_override_and_restart_path():
    facility = FacilityData(
        run_id="run-live",
        record_key="record-ICU-1",
        facility_id="facility-ICU-1",
        facility_name="ICU Candidate 1",
        region="Bihar",
        capability="ICU",
        rank=1,
        support_tier="strong_support",
        support_field_count=4,
        support_item_count=4,
        unresolved_item_count=1,
        marks={
            "description": "supports",
            "capability": "supports",
            "equipment": "supports",
            "procedure": "supports",
        },
        receipt=(
            {
                "field": "description",
                "item_index": 0,
                "text": TARGET_TEXT["ICU"],
                "mark": "supports",
                "outcome": "decision",
                "deciding_check": "vocabulary",
                "check_version": "1.0.0",
                "rationale": "Literal ICU support.",
                "attempts": [],
            },
        ),
        source_urls=("https://example.test/ICU/1",),
        unknown_summary="One item remains unresolved; current capability is not independently verified.",
    )
    results = MemoryResults((facility,))
    reviews = MemoryReviews()
    app = create_app(results, reviews)

    options_status, options = asyncio.run(asgi_request(app, "GET", "/api/options"))
    results_status, ranked = asyncio.run(
        asgi_request(app, "GET", "/api/results", query="capability=ICU&region=Bihar")
    )
    receipt_status, receipt = asyncio.run(
        asgi_request(app, "GET", "/api/receipts/record-ICU-1", query="capability=ICU")
    )
    review_status, saved = asyncio.run(
        asgi_request(
            app,
            "POST",
            "/api/reviews",
            body={
                "record_key": "record-ICU-1",
                "capability": "ICU",
                "decision": "overridden",
                "note": "The listing is out of date.",
            },
            headers=(("x-forwarded-user", "reviewer@example.test"),),
        )
    )

    assert options_status == 200
    assert options["model_requests"] == 0
    assert options["capabilities"] == ["ICU", "maternity", "emergency", "oncology", "trauma", "NICU"]
    assert results_status == 200
    assert [item["record_key"] for item in ranked["facilities"]] == ["record-ICU-1"]
    assert ranked["facilities"][0]["rank"] == 1
    assert receipt_status == 200
    assert receipt["receipt"][0]["field"] == "description"
    assert receipt["receipt"][0]["item_index"] == 0
    assert receipt["source_urls"] == ["https://example.test/ICU/1"]
    assert receipt["receipt"][0]["deciding_check"] == "vocabulary"
    assert "unresolved" in receipt["unknown_summary"]
    assert review_status == 201
    assert saved["decision"] == "overridden"
    assert saved["note"] == "The listing is out of date."
    assert saved["run_id"] == "run-live"
    assert saved["system_verdict"] == "strong_support"

    restarted_app = create_app(results, reviews)
    restart_status, persisted = asyncio.run(
        asgi_request(
            restarted_app,
            "GET",
            "/api/reviews/record-ICU-1",
            query="capability=ICU",
            headers=(("x-forwarded-user", "reviewer@example.test"),),
        )
    )
    assert restart_status == 200
    assert persisted["decision"] == "overridden"
    assert persisted["note"] == "The listing is out of date."
