"""Behaviour tests for the hardened read API and review workflow."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.main import create_app
from app.repositories import (
    FacilityData,
    InMemoryResultStore,
    InMemoryReviewStore,
    facility_from_batch_row,
    translate_receipt_items,
)
from trustdesk.sink import encode_receipt

REVIEWER = {"X-Forwarded-User": "planner@example.org"}


def _facility(
    record_key: str = "rec-1",
    name: str = "Alpha Hospital",
    capability: str = "ICU",
    region: str = "Kerala",
    rank: int = 1,
    support_tier: str = "strong_support",
    receipt: tuple[dict[str, Any], ...] = (),
) -> FacilityData:
    return FacilityData(
        run_id="run-test",
        record_key=record_key,
        facility_id=f"fac-{record_key}",
        facility_name=name,
        region=region,
        capability=capability,
        rank=rank,
        support_tier=support_tier,
        support_field_count=3,
        support_item_count=4,
        unresolved_item_count=1,
        marks={"description": "supports", "capability": "supports", "equipment": "supports", "procedure": "missing"},
        receipt=receipt
        or (
            {
                "field": "description",
                "item_index": 0,
                "text": "Has an ICU.",
                "outcome": "decision",
                "mark": "supports",
                "deciding_check": "vocabulary",
                "check_version": "1.0.0",
                "rationale": "Mentions ICU.",
                "attempts": [],
            },
        ),
        source_urls=("https://example.org/row",),
        unknown_summary="1 evidence item(s) unresolved by the configured checks.",
    )


def _client(
    facilities: tuple[FacilityData, ...] | None = None,
    result_store: InMemoryResultStore | None = None,
    review_store: InMemoryReviewStore | None = None,
) -> tuple[TestClient, InMemoryResultStore, InMemoryReviewStore]:
    results = result_store or InMemoryResultStore(facilities=facilities or (_facility(),))
    reviews = review_store or InMemoryReviewStore()
    return TestClient(create_app(results, reviews)), results, reviews


# --- results: filtering, ordering, separation ---


def test_results_filters_by_capability_and_region() -> None:
    client, _, _ = _client(
        (
            _facility("rec-1"),
            _facility("rec-2", capability="maternity"),
            _facility("rec-3", region="Bihar"),
        )
    )
    payload = client.get("/api/results", params={"capability": "ICU", "region": "Kerala"}).json()
    assert [item["record_key"] for item in payload["facilities"]] == ["rec-1"]
    assert payload["ranking_rule"].startswith("Ranked by strength of record support")


def test_results_orders_ranked_facilities_deterministically_with_ties() -> None:
    client, _, _ = _client(
        (
            _facility("rec-b", name="Beta Hospital", rank=2),
            _facility("rec-a2", name="Alpha Hospital", rank=1),
            _facility("rec-a1", name="Alpha Hospital", rank=1),
        )
    )
    payload = client.get("/api/results", params={"capability": "ICU", "region": "Kerala"}).json()
    assert [item["record_key"] for item in payload["facilities"]] == ["rec-a1", "rec-a2", "rec-b"]


def test_results_separates_unranked_states_from_the_ranking() -> None:
    client, _, _ = _client(
        (
            _facility("rec-1"),
            _facility("rec-2", support_tier="conflicting", rank=0),
            _facility("rec-3", support_tier="not_enough_data", rank=0),
            _facility("rec-4", support_tier="could_not_check", rank=0),
        )
    )
    payload = client.get("/api/results", params={"capability": "ICU", "region": "Kerala"}).json()
    assert [item["record_key"] for item in payload["facilities"]] == ["rec-1"]
    assert {item["support_tier"] for item in payload["unranked"]} == {
        "conflicting",
        "not_enough_data",
        "could_not_check",
    }


def test_results_rejects_unknown_capability() -> None:
    client, _, _ = _client()
    assert client.get("/api/results", params={"capability": "dialysis", "region": "Kerala"}).status_code == 422


def test_results_failure_is_generic_and_read_only() -> None:
    client, _, _ = _client(result_store=InMemoryResultStore(fail_with=RuntimeError("secret table down")))
    response = client.get("/api/results", params={"capability": "ICU", "region": "Kerala"})
    assert response.status_code == 503
    assert "secret" not in response.text


# --- receipts ---


def test_receipt_lookup_returns_detail() -> None:
    client, _, _ = _client()
    payload = client.get("/api/receipts/rec-1", params={"capability": "ICU"}).json()
    assert payload["record_key"] == "rec-1"
    assert payload["receipt"][0]["deciding_check"] == "vocabulary"
    assert payload["source_urls"] == ["https://example.org/row"]


def test_receipt_not_found_is_404() -> None:
    client, _, _ = _client()
    assert client.get("/api/receipts/rec-404", params={"capability": "ICU"}).status_code == 404


def test_sql_injection_shaped_record_key_is_treated_as_data() -> None:
    client, _, _ = _client()
    hostile = "rec-1'; DROP TABLE reviews; --"
    assert client.get(f"/api/receipts/{hostile}", params={"capability": "ICU"}).status_code == 404


# --- batch receipt translation (the Phase 6 -> UI contract) ---


def test_translate_receipt_items_restores_ui_keys() -> None:
    items = [
        {
            "field": "description",
            "item_index": 0,
            "text": "Has an ICU.",
            "final_outcome": "decision",
            "mark": "supports",
            "deciding_check": "vocabulary",
            "referee": {"outcome": "agree", "method": "independent_lexicon", "rationale": "x", "version": "1.0.0"},
            "attempts": [
                {"check_id": "presence", "check_version": "1.0.0", "outcome": "abstention", "rationale": "has text"},
                {"check_id": "vocabulary", "check_version": "1.0.0", "outcome": "decision", "rationale": "mentions"},
            ],
        },
        {
            "field": "procedure",
            "item_index": 0,
            "text": None,
            "final_outcome": "processing_failure",
            "mark": None,
            "deciding_check": None,
            "referee": None,
            "attempts": [
                {
                    "check_id": "vocabulary",
                    "check_version": "1.0.0",
                    "outcome": "processing_failure",
                    "rationale": "broke",
                },
            ],
        },
    ]
    decided, failed = translate_receipt_items(items)
    assert decided["outcome"] == "decision"
    assert decided["check_version"] == "1.0.0"
    assert decided["rationale"] == "mentions"
    assert decided["referee"]["outcome"] == "agree"
    assert decided["attempts"][0]["check_id"] == "presence"
    assert failed["outcome"] == "processing_failure"
    assert failed["rationale"] == "broke"


def test_facility_from_batch_row_decodes_and_translates() -> None:
    receipt_json = (
        '{"capability":"ICU","items":[{"field":"description","item_index":0,"text":"ICU here.",'
        '"final_outcome":"decision","mark":"supports","deciding_check":"vocabulary","referee":null,'
        '"attempts":[{"check_id":"vocabulary","check_version":"1.0.0","outcome":"decision","rationale":"m"}]},'
        '{"field":"capability","item_index":0,"text":"x","final_outcome":"abstention","mark":null,'
        '"deciding_check":null,"referee":null,"attempts":[]}],'
        '"source_urls":["https://example.org/a"],"record_key":"rec-9"}'
    )
    row = {
        "run_id": "run-9",
        "record_key": "rec-9",
        "facility_id": "fac-9",
        "facility_name": "Ninth",
        "region": "Kerala",
        "capability": "ICU",
        "verdict": "limited_support",
        "rank": "3",
        "support_item_count": "1",
        "mark_description": "supports",
        "mark_capability": None,
        "mark_equipment": "missing",
        "mark_procedure": "missing",
        "receipt_json": encode_receipt(receipt_json),
    }
    facility = facility_from_batch_row(row)
    assert facility.support_tier == "limited_support"
    assert facility.rank == 3
    assert facility.receipt[0]["outcome"] == "decision"
    assert facility.receipt[0]["check_version"] == "1.0.0"
    assert facility.unresolved_item_count == 1
    assert facility.marks["capability"] == "unresolved"
    assert facility.support_field_count == 1
    assert "unresolved" in facility.unknown_summary
    assert facility.source_urls == ("https://example.org/a",)


# --- reviews: identity, snapshotting, replacement, origin ---


def _review_body(decision: str = "confirmed", note: str | None = None) -> dict[str, Any]:
    return {"record_key": "rec-1", "capability": "ICU", "decision": decision, "note": note}


def test_review_requires_forwarded_identity_not_body() -> None:
    client, _, _ = _client()
    assert client.post("/api/reviews", json=_review_body()).status_code == 401
    spoofed = {**_review_body(), "reviewer": "attacker"}
    response = client.post("/api/reviews", json=spoofed, headers=REVIEWER)
    assert response.status_code == 201
    assert response.json()["system_verdict"] == "strong_support"


def test_review_snapshots_assessment_and_deciding_checks() -> None:
    client, _, reviews = _client()
    client.post("/api/reviews", json=_review_body(), headers=REVIEWER)
    (saved,) = reviews.saved
    assert saved.run_id == "run-test"
    assert saved.system_verdict == "strong_support"
    assert saved.system_deciding_checks == "vocabulary"
    assert saved.reviewer == "planner@example.org"


def test_override_requires_note_and_replaces_confirm() -> None:
    client, _, _ = _client()
    assert client.post("/api/reviews", json=_review_body("overridden"), headers=REVIEWER).status_code == 422
    client.post("/api/reviews", json=_review_body("confirmed"), headers=REVIEWER)
    client.post("/api/reviews", json=_review_body("overridden", "Wrong: ICU closed."), headers=REVIEWER)
    latest = client.get("/api/reviews/rec-1", params={"capability": "ICU"}, headers=REVIEWER).json()
    assert latest["decision"] == "overridden"
    assert latest["note"] == "Wrong: ICU closed."


def test_duplicate_confirm_is_allowed_and_latest_wins() -> None:
    client, _, reviews = _client()
    assert client.post("/api/reviews", json=_review_body(), headers=REVIEWER).status_code == 201
    assert client.post("/api/reviews", json=_review_body(), headers=REVIEWER).status_code == 201
    assert len(reviews.saved) == 2


def test_review_for_unknown_receipt_is_404() -> None:
    client, _, _ = _client()
    body = {**_review_body(), "record_key": "rec-404"}
    assert client.post("/api/reviews", json=body, headers=REVIEWER).status_code == 404


def test_cross_origin_review_write_is_rejected() -> None:
    client, _, reviews = _client()
    headers = {**REVIEWER, "Origin": "https://evil.example.net", "X-Forwarded-Host": "app.example.org"}
    assert client.post("/api/reviews", json=_review_body(), headers=headers).status_code == 403
    assert reviews.saved == ()


def test_same_origin_review_write_is_accepted() -> None:
    client, _, _ = _client()
    headers = {**REVIEWER, "Origin": "https://app.example.org", "X-Forwarded-Host": "app.example.org"}
    assert client.post("/api/reviews", json=_review_body(), headers=headers).status_code == 201


def test_review_write_failure_does_not_break_reads() -> None:
    client, _, _ = _client(review_store=InMemoryReviewStore(fail_with=RuntimeError("lakebase down")))
    response = client.post("/api/reviews", json=_review_body(), headers=REVIEWER)
    assert response.status_code == 503
    assert "lakebase" not in response.text.lower()
    assert client.get("/api/results", params={"capability": "ICU", "region": "Kerala"}).status_code == 200


# --- methods, options, and hygiene ---


def test_methods_endpoint_serves_precomputed_metrics() -> None:
    client, _, _ = _client()
    payload = client.get("/api/methods").json()
    assert "ranking_rule" in payload
    assert payload["referee"] is None or "totals" in payload["referee"]
    assert "accuracy" not in payload["note"] or "never" in payload["note"]


def test_options_lists_capabilities_and_regions() -> None:
    client, _, _ = _client((_facility(), _facility("rec-2", capability="maternity", region="Bihar")))
    payload = client.get("/api/options").json()
    assert payload["capabilities"] == ["ICU", "maternity"]
    assert payload["regions"] == ["Bihar", "Kerala"]


def test_handlers_never_import_the_check_pipeline_or_model_client() -> None:
    import app.main as app_main

    source = Path(app_main.__file__).read_text() + Path(app_main.__file__).with_name("repositories.py").read_text()
    for forbidden in ("trustdesk.ladder", "trustdesk.llm_check", "trustdesk.referee", "load_checks", "run_checks"):
        assert forbidden not in source


@pytest.mark.parametrize("path", ["/api/results", "/api/receipts/rec-1"])
def test_read_endpoints_do_not_require_identity(path: str) -> None:
    client, _, _ = _client()
    response = client.get(path, params={"capability": "ICU", "region": "Kerala"})
    assert response.status_code in (200, 404)


# --- production adapters against faked Databricks responses ---


def _response(columns: tuple[str, ...], rows: list[list[Any]]) -> Any:
    from databricks.sdk.service.sql import (
        ColumnInfo,
        ResultData,
        ResultManifest,
        ResultSchema,
        StatementResponse,
        StatementState,
        StatementStatus,
    )

    return StatementResponse(
        status=StatementStatus(state=StatementState.SUCCEEDED),
        manifest=ResultManifest(schema=ResultSchema(columns=[ColumnInfo(name=name) for name in columns])),
        result=ResultData(data_array=rows),
    )


class _FakeExecution:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.statements: list[str] = []

    def execute_statement(self, statement: str, warehouse_id: str, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return self.responses.pop(0)


class _FakeWorkspace:
    def __init__(self, responses: list[Any]) -> None:
        self.statement_execution = _FakeExecution(responses)


_LEGACY_COLUMNS = (
    "run_id", "record_key", "facility_id", "facility_name", "region", "capability", "rank",
    "support_tier", "support_field_count", "support_item_count", "unresolved_item_count",
    "marks_json", "receipt_json", "source_urls_json", "unknown_summary",
)


def _legacy_row(record_key: str = "rec-1", name: str = "Alpha Hospital", rank: str = "1") -> list[Any]:
    receipt = (
        '[{"field":"description","item_index":0,"text":"ICU here.","outcome":"decision",'
        '"mark":"supports","deciding_check":"vocabulary","check_version":"1.0.0",'
        '"rationale":"m","attempts":[]}]'
    )
    return [
        "run-w", record_key, f"fac-{record_key}", name, "Kerala", "ICU", rank,
        "strong_support", "3", "4", "1",
        '{"description":"supports","capability":"supports","equipment":"supports","procedure":"missing"}',
        receipt, '["https://example.org/row"]', "1 item unresolved.",
    ]


_BATCH_COLUMNS = (
    "run_id", "record_key", "facility_id", "facility_name", "region", "capability",
    "verdict", "rank", "support_item_count",
    "mark_description", "mark_capability", "mark_equipment", "mark_procedure", "receipt_json",
)


def _batch_row(record_key: str = "rec-9", verdict: str = "strong_support", rank: Any = "1") -> list[Any]:
    receipt_json = (
        '{"capability":"ICU","items":[{"field":"description","item_index":0,"text":"ICU here.",'
        '"final_outcome":"decision","mark":"supports","deciding_check":"vocabulary","referee":null,'
        '"attempts":[{"check_id":"vocabulary","check_version":"1.0.0","outcome":"decision","rationale":"m"}]}],'
        '"source_urls":["https://example.org/a"],"record_key":"' + record_key + '"}'
    )
    return [
        "run-b", record_key, f"fac-{record_key}", "Ninth", "Kerala", "ICU",
        verdict, rank, "1", "supports", "supports", "supports", "missing",
        encode_receipt(receipt_json),
    ]


def test_legacy_production_store_search_and_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as app_main

    responses = [
        _response(_LEGACY_COLUMNS, [_legacy_row(), _legacy_row("rec-2", "Beta Hospital", "2")]),
        _response(_LEGACY_COLUMNS, [_legacy_row()]),
        _response(_LEGACY_COLUMNS, []),
    ]
    monkeypatch.setattr(app_main, "WorkspaceClient", lambda: _FakeWorkspace(responses))
    store = app_main.DatabricksResultStore(warehouse_id="wh", table="workspace.default.results")
    facilities = store.search("ICU", "Kerala")
    assert [item.record_key for item in facilities] == ["rec-1", "rec-2"]
    assert facilities[0].support_tier == "strong_support"
    found = store.receipt("rec-1", "ICU")
    assert found is not None and found.receipt[0]["deciding_check"] == "vocabulary"
    assert store.receipt("rec-404", "ICU") is None


def test_legacy_production_store_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as app_main

    responses = [
        _response(("run_id", "capability", "region"), [["run-w", "ICU", "Kerala"], ["run-w", "maternity", "Bihar"]]),
    ]
    monkeypatch.setattr(app_main, "WorkspaceClient", lambda: _FakeWorkspace(responses))
    store = app_main.DatabricksResultStore(warehouse_id="wh", table="workspace.default.results")
    payload = store.options()
    assert payload["capabilities"] == ["ICU", "maternity"]
    assert payload["regions"] == ["Bihar", "Kerala"]


def test_active_run_production_store_search_receipt_and_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.repositories as repositories

    responses = [
        _response(_BATCH_COLUMNS, [_batch_row(), _batch_row("rec-8", "not_enough_data", None)]),
        _response(_BATCH_COLUMNS, [_batch_row()]),
        _response(_BATCH_COLUMNS, []),
        _response(("run_id", "capability", "region"), [["run-b", "ICU", "Kerala"]]),
    ]
    monkeypatch.setattr(repositories, "WorkspaceClient", lambda: _FakeWorkspace(responses))
    store = repositories.ActiveRunResultStore(warehouse_id="wh")
    facilities = store.search("ICU", "Kerala")
    assert [item.support_tier for item in facilities] == ["strong_support", "not_enough_data"]
    assert facilities[0].receipt[0]["outcome"] == "decision"
    found = store.receipt("rec-9", "ICU")
    assert found is not None and found.rank == 1
    assert store.receipt("rec-404", "ICU") is None
    assert store.options()["capabilities"] == ["ICU"]


def test_production_and_in_memory_stores_share_the_endpoint_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as app_main

    responses = [_response(_LEGACY_COLUMNS, [_legacy_row()])]
    monkeypatch.setattr(app_main, "WorkspaceClient", lambda: _FakeWorkspace(responses))
    production = app_main.DatabricksResultStore(warehouse_id="wh", table="workspace.default.results")
    memory = InMemoryResultStore(facilities=(_facility(),))
    for store in (production, memory):
        client = TestClient(create_app(store, InMemoryReviewStore()))
        payload = client.get("/api/results", params={"capability": "ICU", "region": "Kerala"}).json()
        assert len(payload["facilities"]) == 1
        assert payload["facilities"][0]["support_tier"] == "strong_support"


def test_default_result_store_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as app_main
    import app.repositories as repositories

    monkeypatch.delenv("TRUSTDESK_RESULTS_SOURCE", raising=False)
    assert isinstance(app_main.default_result_store(), app_main.DatabricksResultStore)
    monkeypatch.setenv("TRUSTDESK_RESULTS_SOURCE", "batch")
    monkeypatch.setenv("TRUSTDESK_SQL_WAREHOUSE_ID", "wh")
    assert isinstance(app_main.default_result_store(), repositories.ActiveRunResultStore)


def test_database_settings_require_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as app_main

    for name in ("TRUSTDESK_POSTGRES_ENDPOINT", "PGHOST", "PGDATABASE", "PGUSER", "PGPORT", "PGSSLMODE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="TRUSTDESK_POSTGRES_ENDPOINT"):
        app_main.database_settings()
    monkeypatch.setenv("TRUSTDESK_POSTGRES_ENDPOINT", "pg")
    monkeypatch.setenv("PGHOST", "h")
    monkeypatch.setenv("PGDATABASE", "d")
    monkeypatch.setenv("PGUSER", "u")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGSSLMODE", "require")
    assert app_main.database_settings().port == 5432


def test_decode_receipt_rejects_bad_envelopes() -> None:
    from app.repositories import decode_receipt

    with pytest.raises(ValueError):
        decode_receipt('{"codec":"zip","payload":"x"}')
    with pytest.raises(ValueError):
        decode_receipt('{"codec":"gzip+base64+json","payload":7}')


def test_review_row_mapping_defaults_missing_deciding_checks() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    import app.main as app_main

    row = (uuid4(), "rec-1", "ICU", "confirmed", None, "run-w", "strong_support", None, "p@x.org",
           datetime.now(UTC))
    review = app_main._review_from_row(row)
    assert review.system_deciding_checks == ""
    assert review.reviewer == "p@x.org"


# --- review-driven regression tests: quarantine, truncation, read-path migration ---


def test_active_run_store_survives_quarantine_shaped_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.repositories as repositories

    quarantine_payload = (
        '{"record_key":"rec-q","reasons":["unparseable capability field"],'
        '"pipeline_run_id":"run-b","computed_at":"2026-07-19T00:00:00+00:00"}'
    )
    row = _batch_row("rec-q", "could_not_check", None)
    row[-1] = encode_receipt(quarantine_payload)
    responses = [_response(_BATCH_COLUMNS, [_batch_row(), row])]
    monkeypatch.setattr(repositories, "WorkspaceClient", lambda: _FakeWorkspace(responses))
    store = repositories.ActiveRunResultStore(warehouse_id="wh")
    facilities = store.search("ICU", "Kerala")
    assert [item.support_tier for item in facilities] == ["strong_support", "could_not_check"]
    assert facilities[1].receipt == ()


def test_active_run_join_excludes_quarantine_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.repositories as repositories

    responses = [_response(_BATCH_COLUMNS, [_batch_row()])]
    fake = _FakeWorkspace(responses)
    monkeypatch.setattr(repositories, "WorkspaceClient", lambda: fake)
    repositories.ActiveRunResultStore(warehouse_id="wh").search("ICU", "Kerala")
    assert "r.receipt_kind = 'claim_evidence'" in fake.statement_execution.statements[0]


def test_truncated_result_fails_loudly_not_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.repositories as repositories

    truncated = _response(_BATCH_COLUMNS, [_batch_row()])
    assert truncated.manifest is not None
    truncated.manifest.truncated = True
    monkeypatch.setattr(repositories, "WorkspaceClient", lambda: _FakeWorkspace([truncated]))
    store = repositories.ActiveRunResultStore(warehouse_id="wh")
    with pytest.raises(RuntimeError, match="truncated"):
        store.search("ICU", "Kerala")


def test_review_read_path_runs_schema_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    import app.main as app_main

    executed: list[str] = []

    class _FakeCursor:
        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, statement: str, params: object = None) -> None:
            executed.append(statement.strip().split()[0] + " " + statement.strip().split()[1])

        def fetchone(self) -> None:
            return None

    class _FakeConnection:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def commit(self) -> None:
            return None

    @contextmanager
    def _fake_connection() -> Any:
        yield _FakeConnection()

    monkeypatch.setattr(app_main, "database_connection", _fake_connection)
    store = app_main.LakebaseReviewStore()
    assert store.latest("rec-1", "ICU", "p@x.org") is None
    assert executed[:3] == ["CREATE SCHEMA", "CREATE TABLE", "ALTER TABLE"]
    migrations_after_first_call = len(executed)
    store.latest("rec-1", "ICU", "p@x.org")
    assert len(executed) == migrations_after_first_call + 1  # only the SELECT, no re-migration
