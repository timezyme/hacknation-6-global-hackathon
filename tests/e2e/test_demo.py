"""End-to-end contract for the exact one-minute judged workflow."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[2]))
from app.main import create_app
from app.repositories import FacilityData, InMemoryResultStore, InMemoryReviewStore

PLANNER = {"X-Forwarded-User": "planner@example.org"}


def _item(
    field: str,
    outcome: str,
    mark: str | None,
    text: str | None,
    referee: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "field": field,
        "item_index": 0,
        "text": text,
        "outcome": outcome,
        "mark": mark,
        "deciding_check": "vocabulary" if outcome == "decision" else None,
        "check_version": "1.0.0",
        "rationale": "Mentions the capability." if outcome == "decision" else "Could not settle this item.",
        "referee": referee,
        "attempts": [
            {"check_id": "presence", "check_version": "1.0.0", "outcome": "abstention", "rationale": "has text"},
            {"check_id": "vocabulary", "check_version": "1.0.0", "outcome": outcome, "rationale": "checked"},
        ],
    }


def _facility(
    record_key: str,
    name: str,
    rank: int,
    support_tier: str,
    receipt: tuple[dict[str, Any], ...],
    marks: dict[str, str],
) -> FacilityData:
    unresolved = sum(1 for item in receipt if item["outcome"] != "decision")
    return FacilityData(
        run_id="run-demo",
        record_key=record_key,
        facility_id=f"fac-{record_key}",
        facility_name=name,
        region="Kerala",
        capability="ICU",
        rank=rank,
        support_tier=support_tier,
        support_field_count=sum(1 for mark in marks.values() if mark == "supports"),
        support_item_count=sum(1 for item in receipt if item["mark"] == "supports"),
        unresolved_item_count=unresolved,
        marks=marks,
        receipt=receipt,
        source_urls=("https://example.org/row",),
        unknown_summary=f"{unresolved} evidence item(s) unresolved by the configured checks.",
    )


FACILITIES = (
    _facility(
        "rec-top",
        "Alpha Hospital",
        1,
        "strong_support",
        (
            _item(
                "description",
                "decision",
                "supports",
                "Six bed intensive care unit.",
                referee={
                    "outcome": "agree",
                    "method": "independent_lexicon",
                    "rationale": "corroborated",
                    "version": "1.0.0",
                },
            ),
            _item("capability", "decision", "supports", "ICU"),
        ),
        {"description": "supports", "capability": "supports", "equipment": "supports", "procedure": "missing"},
    ),
    _facility(
        "rec-unresolved",
        "Beta Hospital",
        0,
        "not_enough_data",
        (_item("description", "abstention", None, "General hospital."),),
        {"description": "unresolved", "capability": "missing", "equipment": "missing", "procedure": "missing"},
    ),
    _facility(
        "rec-broken",
        "Gamma Hospital",
        0,
        "could_not_check",
        (_item("description", "processing_failure", None, None),),
        {"description": "failed", "capability": "unresolved", "equipment": "unresolved", "procedure": "missing"},
    ),
)


def test_one_minute_workflow_end_to_end() -> None:
    reviews = InMemoryReviewStore()
    client = TestClient(create_app(InMemoryResultStore(facilities=FACILITIES, run_id="run-demo"), reviews))

    # Step 1: capability and region come from the active run.
    options = client.get("/api/options").json()
    assert "ICU" in options["capabilities"]
    assert "Kerala" in options["regions_by_capability"]["ICU"]

    # Step 2: ranked list is stable and separated from the honest states.
    results = client.get("/api/results", params={"capability": "ICU", "region": "Kerala"}).json()
    assert [item["record_key"] for item in results["facilities"]] == ["rec-top"]
    assert {item["support_tier"] for item in results["unranked"]} == {"not_enough_data", "could_not_check"}
    assert results["ranking_rule"].startswith("Ranked by strength")

    # Step 3: the receipt shows the exact item, deciding check, referee, and sources.
    receipt = client.get("/api/receipts/rec-top", params={"capability": "ICU"}).json()
    decided = receipt["receipt"][0]
    assert decided["text"] == "Six bed intensive care unit."
    assert decided["deciding_check"] == "vocabulary"
    assert decided["referee"]["outcome"] == "agree"
    assert receipt["source_urls"] == ["https://example.org/row"]

    # Step 4: an unresolved record and a processing-failure record stay inspectable.
    unresolved = client.get("/api/receipts/rec-unresolved", params={"capability": "ICU"}).json()
    assert unresolved["receipt"][0]["outcome"] == "abstention"
    broken = client.get("/api/receipts/rec-broken", params={"capability": "ICU"}).json()
    assert broken["receipt"][0]["outcome"] == "processing_failure"

    # Step 5: override with a note, then reload and see it persisted.
    saved = client.post(
        "/api/reviews",
        json={
            "record_key": "rec-top",
            "capability": "ICU",
            "decision": "overridden",
            "note": "ICU closed since March.",
        },
        headers=PLANNER,
    )
    assert saved.status_code == 201
    reloaded = client.get("/api/reviews/rec-top", params={"capability": "ICU"}, headers=PLANNER).json()
    assert reloaded["decision"] == "overridden"
    assert reloaded["note"] == "ICU closed since March."
    assert reloaded["system_verdict"] == "strong_support"

    # Step 6: measurements come precomputed; no model was ever called.
    methods = client.get("/api/methods").json()
    assert "ranking_rule" in methods
    assert client.get("/api/options").json()["model_requests"] == 0


def test_security_headers_are_enforced() -> None:
    client = TestClient(create_app(InMemoryResultStore(facilities=FACILITIES), InMemoryReviewStore()))
    response = client.get("/api/health")
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_page_contains_no_hardcoded_facility_catalog() -> None:
    page = (Path(__file__).parents[2] / "app" / "index.html").read_text()
    assert "Alpha Hospital" not in page
    for facility in FACILITIES:
        assert facility.record_key not in page
