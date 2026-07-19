"""Repository boundaries for the Trust Desk app.

Interfaces, in-memory adapters, and production adapters over the Phase 6 batch tables.
Kept free of trustdesk imports on purpose: the deployed Databricks App bundle installs
only the app's own dependencies, so the handful of shared constants and the receipt
codec are duplicated here rather than imported from the batch pipeline.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem, StatementResponse

CAPABILITIES = ("ICU", "maternity", "emergency", "oncology", "trauma", "NICU")
TABLE_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
# Mirrors src/trustdesk/sink.py; a rename there is a contract change, not a refactor.
VERDICTS_TABLE = "workspace.default.trustdesk_verdicts"
RECEIPTS_TABLE = "workspace.default.trustdesk_receipts"
ACTIVE_RUN_TABLE = "workspace.default.trustdesk_active_run"
RANKED_TIERS = ("strong_support", "limited_support")
FIELDS = ("description", "capability", "equipment", "procedure")
RANKING_RULE = (
    "Ranked by strength of record support: support tier, then distinct supporting "
    "evidence items, then facility name A-Z, then record key. Never facility quality."
)


@dataclass(frozen=True)
class FacilityData:
    """One active result in the shape consumed by the API and UI."""

    run_id: str
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
    marks: dict[str, str]
    receipt: tuple[dict[str, Any], ...]
    source_urls: tuple[str, ...]
    unknown_summary: str
    similar: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewRecord:
    """Immutable snapshot of one reviewer response to one displayed result."""

    review_id: UUID
    record_key: str
    capability: str
    decision: str
    note: str | None
    run_id: str
    system_verdict: str
    system_deciding_checks: str
    reviewer: str
    created_at: datetime


class ResultStore(Protocol):
    def options(self) -> dict[str, object]: ...

    def search(self, capability: str, region: str) -> tuple[FacilityData, ...]: ...

    def receipt(self, record_key: str, capability: str) -> FacilityData | None: ...


class ReviewStore(Protocol):
    def save(self, review: ReviewRecord) -> ReviewRecord: ...

    def latest(self, record_key: str, capability: str, reviewer: str) -> ReviewRecord | None: ...


def decode_receipt(stored_json: str) -> str:
    """Mirror of the sink's receipt codec (gzip+base64+json envelope)."""
    envelope = json.loads(stored_json)
    if not isinstance(envelope, dict) or envelope.get("codec") != "gzip+base64+json":
        raise ValueError("unsupported receipt encoding")
    payload = envelope.get("payload")
    if not isinstance(payload, str):
        raise ValueError("invalid receipt encoding")
    return gzip.decompress(base64.b64decode(payload, validate=True)).decode()


def translate_receipt_items(items: list[Any]) -> tuple[dict[str, Any], ...]:
    """Adapt batch receipt items to the UI contract.

    The batch writes `final_outcome` and keeps per-item check identity only inside
    `attempts`; the UI filters on `outcome` and renders `check_version`/`rationale`
    directly. The deciding attempt supplies both; failures fall back to the last
    attempt so a processing failure still shows which check broke and why.
    """
    translated: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("receipt item is malformed")
        attempts = item.get("attempts")
        attempts = attempts if isinstance(attempts, list) else []
        deciding = next(
            (attempt for attempt in attempts if isinstance(attempt, dict) and attempt.get("outcome") == "decision"),
            None,
        )
        last = attempts[-1] if attempts and isinstance(attempts[-1], dict) else None
        chosen: Mapping[str, Any] = deciding or last or {}
        translated.append(
            {
                **item,
                "outcome": item.get("final_outcome"),
                "check_version": chosen.get("check_version"),
                "rationale": chosen.get("rationale"),
            }
        )
    return tuple(translated)


def _marks_map(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        field_name: value if isinstance(value := row.get(f"mark_{field_name}"), str) else "unresolved"
        for field_name in FIELDS
    }


def _unknown_summary(marks: Mapping[str, str], unresolved_items: int) -> str:
    blank = sorted(name for name, mark in marks.items() if mark == "missing")
    parts: list[str] = []
    if unresolved_items:
        parts.append(f"{unresolved_items} evidence item(s) unresolved by the configured checks")
    if blank:
        parts.append(f"blank field(s): {', '.join(blank)}")
    return "; ".join(parts) + "." if parts else "Every evidence item was settled by a configured check."


def _statement_rows(response: StatementResponse) -> tuple[dict[str, Any], ...]:
    state = getattr(response.status, "state", None)
    if state is None or state.value != "SUCCEEDED":
        raise RuntimeError("result query failed")
    if response.result is None:
        return ()
    if response.manifest is None or response.manifest.schema is None:
        raise RuntimeError("result query returned no schema")
    if response.manifest.truncated:
        # A silently cut list would misrank facilities; failing loudly is honest.
        raise RuntimeError("result query was truncated")
    schema_columns = response.manifest.schema.columns or []
    columns = tuple(column.name for column in schema_columns if column.name is not None)
    if len(columns) != len(schema_columns):
        raise RuntimeError("result query returned an unnamed column")
    return tuple(dict(zip(columns, values, strict=True)) for values in response.result.data_array or [])


def _text(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str):
        raise RuntimeError("result row is malformed")
    return value


def facility_from_batch_row(row: Mapping[str, Any]) -> FacilityData:
    """Build UI-shaped facility data from one joined verdicts+receipts row."""
    raw_receipt = row.get("receipt_json")
    items: list[Any] = []
    source_urls: tuple[str, ...] = ()
    similar: dict[str, Any] | None = None
    if isinstance(raw_receipt, str) and raw_receipt:
        payload = json.loads(decode_receipt(raw_receipt))
        if not isinstance(payload, dict):
            raise RuntimeError("result receipt is malformed")
        # Quarantine receipts carry no evidence items; render them as an empty
        # receipt rather than failing the whole region view.
        raw_items = payload.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        sources = payload.get("source_urls")
        source_urls = tuple(sources) if isinstance(sources, list) else ()
        raw_similar = payload.get("similar")
        similar = raw_similar if isinstance(raw_similar, dict) else None
    receipt = translate_receipt_items(items)
    marks = _marks_map(row)
    unresolved = sum(1 for item in receipt if item.get("outcome") != "decision")
    rank_value = row.get("rank")
    return FacilityData(
        run_id=_text(row, "run_id"),
        record_key=_text(row, "record_key"),
        facility_id=row.get("facility_id") or "",
        facility_name=row.get("facility_name") or "(unnamed facility)",
        region=row.get("region") or "",
        capability=_text(row, "capability"),
        rank=int(rank_value) if rank_value is not None else 0,
        support_tier=_text(row, "verdict"),
        support_field_count=sum(1 for mark in marks.values() if mark == "supports"),
        support_item_count=int(row.get("support_item_count") or 0),
        unresolved_item_count=unresolved,
        marks=marks,
        receipt=receipt,
        source_urls=source_urls,
        unknown_summary=_unknown_summary(marks, unresolved),
        similar=similar,
    )


class ActiveRunResultStore:
    """Read the atomic active batch run published by the Phase 6 sink."""

    def __init__(self, warehouse_id: str) -> None:
        self.warehouse_id = warehouse_id

    def _query(
        self,
        statement: str,
        parameters: tuple[StatementParameterListItem, ...] = (),
    ) -> tuple[dict[str, Any], ...]:
        response = WorkspaceClient().statement_execution.execute_statement(
            statement,
            self.warehouse_id,
            disposition=Disposition.INLINE,
            format=Format.JSON_ARRAY,
            parameters=list(parameters),
            row_limit=1000,
            wait_timeout="50s",
        )
        return _statement_rows(response)

    _ACTIVE = f"(SELECT run_id FROM {ACTIVE_RUN_TABLE} WHERE pointer_name = 'default' LIMIT 1)"
    # Quarantine receipts carry no evidence items and are excluded here; the
    # verdict row itself still surfaces the could-not-check state.
    _ROW_QUERY = f"""
        SELECT v.run_id, v.record_key, v.facility_id, v.facility_name, v.region, v.capability,
               v.verdict, v.rank, v.support_item_count,
               v.mark_description, v.mark_capability, v.mark_equipment, v.mark_procedure,
               r.receipt_json
        FROM {VERDICTS_TABLE} v
        LEFT JOIN {RECEIPTS_TABLE} r
          ON r.run_id = v.run_id AND r.record_key = v.record_key AND r.capability = v.capability
         AND r.receipt_kind = 'claim_evidence'
        WHERE v.run_id = {_ACTIVE}
    """

    def options(self) -> dict[str, object]:
        rows = self._query(
            f"""SELECT run_id, capability, region FROM {VERDICTS_TABLE}
                WHERE run_id = {self._ACTIVE}
                GROUP BY run_id, capability, region"""
        )
        if not rows:
            raise RuntimeError("no active batch run")
        observed = {_text(row, "capability") for row in rows}
        regions = sorted({region for row in rows if isinstance(region := row.get("region"), str) and region})
        return {
            "run_id": _text(rows[0], "run_id"),
            "capabilities": [capability for capability in CAPABILITIES if capability in observed],
            "regions": regions,
            "regions_by_capability": {
                capability: sorted(
                    {
                        region
                        for row in rows
                        if _text(row, "capability") == capability
                        and isinstance(region := row.get("region"), str)
                        and region
                    }
                )
                for capability in CAPABILITIES
                if capability in observed
            },
            "model_requests": 0,
        }

    def search(self, capability: str, region: str) -> tuple[FacilityData, ...]:
        rows = self._query(
            self._ROW_QUERY
            + """ AND v.capability = :capability AND v.region = :region
                 ORDER BY CASE WHEN v.rank IS NULL THEN 1 ELSE 0 END,
                          v.rank, v.facility_name, v.record_key""",
            (
                StatementParameterListItem(name="capability", value=capability),
                StatementParameterListItem(name="region", value=region),
            ),
        )
        return tuple(facility_from_batch_row(row) for row in rows)

    def receipt(self, record_key: str, capability: str) -> FacilityData | None:
        rows = self._query(
            self._ROW_QUERY + " AND v.record_key = :record_key AND v.capability = :capability LIMIT 1",
            (
                StatementParameterListItem(name="record_key", value=record_key),
                StatementParameterListItem(name="capability", value=capability),
            ),
        )
        return facility_from_batch_row(rows[0]) if rows else None


@dataclass
class InMemoryResultStore:
    """Deterministic result store for tests and local development."""

    facilities: tuple[FacilityData, ...] = ()
    run_id: str = "run-test"
    fail_with: Exception | None = None

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def options(self) -> dict[str, object]:
        self._check()
        observed = {facility.capability for facility in self.facilities}
        return {
            "run_id": self.run_id,
            "capabilities": [capability for capability in CAPABILITIES if capability in observed],
            "regions": sorted({facility.region for facility in self.facilities}),
            "regions_by_capability": {
                capability: sorted(
                    {facility.region for facility in self.facilities if facility.capability == capability}
                )
                for capability in CAPABILITIES
                if capability in observed
            },
            "model_requests": 0,
        }

    def search(self, capability: str, region: str) -> tuple[FacilityData, ...]:
        self._check()
        matched = [
            facility
            for facility in self.facilities
            if facility.capability == capability and facility.region == region
        ]
        ranked = sorted(
            (facility for facility in matched if facility.support_tier in RANKED_TIERS),
            key=lambda facility: (facility.rank, facility.facility_name, facility.record_key),
        )
        unranked = sorted(
            (facility for facility in matched if facility.support_tier not in RANKED_TIERS),
            key=lambda facility: (facility.facility_name, facility.record_key),
        )
        return (*ranked, *unranked)

    def receipt(self, record_key: str, capability: str) -> FacilityData | None:
        self._check()
        return next(
            (
                facility
                for facility in self.facilities
                if facility.record_key == record_key and facility.capability == capability
            ),
            None,
        )


@dataclass
class InMemoryReviewStore:
    """Review store double preserving insert order per reviewer."""

    saved: tuple[ReviewRecord, ...] = field(default=())
    fail_with: Exception | None = None

    def save(self, review: ReviewRecord) -> ReviewRecord:
        if self.fail_with is not None:
            raise self.fail_with
        self.saved = (*self.saved, replace(review))
        return review

    def latest(self, record_key: str, capability: str, reviewer: str) -> ReviewRecord | None:
        if self.fail_with is not None:
            raise self.fail_with
        matches = [
            review
            for review in self.saved
            if review.record_key == record_key
            and review.capability == capability
            and review.reviewer == reviewer
        ]
        return matches[-1] if matches else None
