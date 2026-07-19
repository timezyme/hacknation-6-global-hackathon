"""Read-only walking-skeleton app with persistent reviewer feedback."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import psycopg
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem, StatementResponse
from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from psycopg import Connection
from pydantic import BaseModel, Field

LOGGER = logging.getLogger("trustdesk.walking_skeleton")
CAPABILITIES = ("ICU", "maternity", "emergency", "oncology", "trauma", "NICU")
TABLE_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
CREATE_APP_SCHEMA = "CREATE SCHEMA IF NOT EXISTS trustdesk"
CREATE_REVIEW_TABLE = """
CREATE TABLE IF NOT EXISTS trustdesk.review_decisions (
    review_id UUID PRIMARY KEY,
    record_key TEXT NOT NULL,
    capability TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT,
    run_id TEXT NOT NULL,
    system_verdict TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
)
"""
INSERT_REVIEW = """
INSERT INTO trustdesk.review_decisions (
    review_id, record_key, capability, decision, note,
    run_id, system_verdict, reviewer, created_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
SELECT_REVIEW = """
SELECT review_id, record_key, capability, decision, note,
       run_id, system_verdict, reviewer, created_at
FROM trustdesk.review_decisions
WHERE record_key = %s AND capability = %s AND reviewer = %s
ORDER BY created_at DESC
LIMIT 1
"""


@dataclass(frozen=True)
class DatabaseSettings:
    endpoint: str
    host: str
    database: str
    user: str
    port: int
    sslmode: str
    application_name: str


@dataclass(frozen=True)
class FacilityData:
    """One active Delta result in the shape consumed by the API and UI."""

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
    reviewer: str
    created_at: datetime


class ResultStore(Protocol):
    def options(self) -> dict[str, object]: ...

    def search(self, capability: str, region: str) -> tuple[FacilityData, ...]: ...

    def receipt(self, record_key: str, capability: str) -> FacilityData | None: ...


class ReviewStore(Protocol):
    def save(self, review: ReviewRecord) -> ReviewRecord: ...

    def latest(self, record_key: str, capability: str, reviewer: str) -> ReviewRecord | None: ...


class HealthResponse(BaseModel):
    status: str


class ReviewRequest(BaseModel):
    record_key: str = Field(min_length=1, max_length=1024)
    capability: str = Field(min_length=2, max_length=40)
    decision: Literal["confirmed", "overridden"]
    note: str | None = Field(default=None, max_length=500)


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def database_settings() -> DatabaseSettings:
    return DatabaseSettings(
        endpoint=required_environment("TRUSTDESK_POSTGRES_ENDPOINT"),
        host=required_environment("PGHOST"),
        database=required_environment("PGDATABASE"),
        user=required_environment("PGUSER"),
        port=int(required_environment("PGPORT")),
        sslmode=required_environment("PGSSLMODE"),
        application_name=os.environ.get("PGAPPNAME", "trustdesk-walking-skeleton"),
    )


@contextmanager
def database_connection() -> Iterator[Connection[Any]]:
    """Use one fresh App OAuth credential for one request-scoped connection."""
    settings = database_settings()
    credential = WorkspaceClient().postgres.generate_database_credential(endpoint=settings.endpoint)
    if not credential.token:
        raise RuntimeError("database credential was unavailable")
    with psycopg.connect(
        host=settings.host,
        dbname=settings.database,
        user=settings.user,
        password=credential.token,
        port=settings.port,
        sslmode=settings.sslmode,
        application_name=settings.application_name,
        connect_timeout=10,
    ) as connection:
        yield connection


def _statement_rows(response: StatementResponse) -> tuple[dict[str, Any], ...]:
    state = getattr(response.status, "state", None)
    if state is None or state.value != "SUCCEEDED":
        raise RuntimeError("result query failed")
    if response.manifest is None or response.manifest.schema is None or response.result is None:
        raise RuntimeError("result query returned no data")
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


def _facility(row: Mapping[str, Any]) -> FacilityData:
    marks = json.loads(_text(row, "marks_json"))
    receipt = json.loads(_text(row, "receipt_json"))
    sources = json.loads(_text(row, "source_urls_json"))
    if (
        not isinstance(marks, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in marks.items())
        or not isinstance(receipt, list)
        or not all(isinstance(item, dict) for item in receipt)
        or not isinstance(sources, list)
        or not all(isinstance(source, str) for source in sources)
    ):
        raise RuntimeError("result receipt is malformed")
    return FacilityData(
        run_id=_text(row, "run_id"),
        record_key=_text(row, "record_key"),
        facility_id=_text(row, "facility_id"),
        facility_name=_text(row, "facility_name"),
        region=_text(row, "region"),
        capability=_text(row, "capability"),
        rank=int(_text(row, "rank")),
        support_tier=_text(row, "support_tier"),
        support_field_count=int(_text(row, "support_field_count")),
        support_item_count=int(_text(row, "support_item_count")),
        unresolved_item_count=int(_text(row, "unresolved_item_count")),
        marks=dict(marks),
        receipt=tuple(receipt),
        source_urls=tuple(sources),
        unknown_summary=_text(row, "unknown_summary"),
    )


class DatabricksResultStore:
    """Read the most recently completed walking-skeleton run from Delta."""

    def __init__(self, warehouse_id: str | None = None, table: str | None = None) -> None:
        self._warehouse_id = warehouse_id
        self._table = table

    @property
    def warehouse_id(self) -> str:
        return self._warehouse_id or required_environment("TRUSTDESK_SQL_WAREHOUSE_ID")

    @property
    def table(self) -> str:
        table = self._table or required_environment("TRUSTDESK_RESULTS_TABLE")
        if not TABLE_NAME.fullmatch(table):
            raise RuntimeError("results table name is invalid")
        return table

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
            row_limit=100,
            wait_timeout="50s",
        )
        return _statement_rows(response)

    def options(self) -> dict[str, object]:
        rows = self._query(
            f"""
            SELECT run_id, capability, region
            FROM {self.table}
            WHERE run_status = 'complete'
              AND published_at = (
                  SELECT MAX(published_at) FROM {self.table} WHERE run_status = 'complete'
              )
            """
        )
        if not rows:
            raise RuntimeError("no completed walking-skeleton run")
        observed = {_text(row, "capability") for row in rows}
        regions_by_capability = {
            capability: sorted(
                {
                    _text(row, "region")
                    for row in rows
                    if _text(row, "capability") == capability
                }
            )
            for capability in CAPABILITIES
            if capability in observed
        }
        return {
            "run_id": _text(rows[0], "run_id"),
            "capabilities": [capability for capability in CAPABILITIES if capability in observed],
            "regions": sorted({_text(row, "region") for row in rows}),
            "regions_by_capability": regions_by_capability,
            "model_requests": 0,
        }

    def search(self, capability: str, region: str) -> tuple[FacilityData, ...]:
        rows = self._query(
            f"""
            SELECT run_id, record_key, facility_id, facility_name, region, capability, rank,
                   support_tier, support_field_count, support_item_count, unresolved_item_count,
                   marks_json, receipt_json, source_urls_json, unknown_summary
            FROM {self.table}
            WHERE run_status = 'complete'
              AND published_at = (
                  SELECT MAX(published_at) FROM {self.table} WHERE run_status = 'complete'
              )
              AND capability = :capability AND region = :region
            ORDER BY rank, facility_name, record_key
            """,
            (
                StatementParameterListItem(name="capability", value=capability),
                StatementParameterListItem(name="region", value=region),
            ),
        )
        return tuple(_facility(row) for row in rows)

    def receipt(self, record_key: str, capability: str) -> FacilityData | None:
        rows = self._query(
            f"""
            SELECT run_id, record_key, facility_id, facility_name, region, capability, rank,
                   support_tier, support_field_count, support_item_count, unresolved_item_count,
                   marks_json, receipt_json, source_urls_json, unknown_summary
            FROM {self.table}
            WHERE run_status = 'complete'
              AND published_at = (
                  SELECT MAX(published_at) FROM {self.table} WHERE run_status = 'complete'
              )
              AND record_key = :record_key AND capability = :capability
            LIMIT 1
            """,
            (
                StatementParameterListItem(name="record_key", value=record_key),
                StatementParameterListItem(name="capability", value=capability),
            ),
        )
        return _facility(rows[0]) if rows else None


def _review_from_row(row: tuple[Any, ...]) -> ReviewRecord:
    return ReviewRecord(
        review_id=row[0],
        record_key=row[1],
        capability=row[2],
        decision=row[3],
        note=row[4],
        run_id=row[5],
        system_verdict=row[6],
        reviewer=row[7],
        created_at=row[8],
    )


class LakebaseReviewStore:
    """Persist immutable review snapshots in the already-bound Lakebase database."""

    def save(self, review: ReviewRecord) -> ReviewRecord:
        with database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(CREATE_APP_SCHEMA)
            cursor.execute(CREATE_REVIEW_TABLE)
            cursor.execute(
                INSERT_REVIEW,
                (
                    review.review_id,
                    review.record_key,
                    review.capability,
                    review.decision,
                    review.note,
                    review.run_id,
                    review.system_verdict,
                    review.reviewer,
                    review.created_at,
                ),
            )
            connection.commit()
        return review

    def latest(self, record_key: str, capability: str, reviewer: str) -> ReviewRecord | None:
        with database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(SELECT_REVIEW, (record_key, capability, reviewer))
            row = cursor.fetchone()
        return _review_from_row(row) if row is not None else None


def _summary(facility: FacilityData) -> dict[str, object]:
    return {
        "run_id": facility.run_id,
        "record_key": facility.record_key,
        "facility_id": facility.facility_id,
        "facility_name": facility.facility_name,
        "region": facility.region,
        "capability": facility.capability,
        "rank": facility.rank,
        "support_tier": facility.support_tier,
        "support_field_count": facility.support_field_count,
        "support_item_count": facility.support_item_count,
        "unresolved_item_count": facility.unresolved_item_count,
        "marks": facility.marks,
    }


def _detail(facility: FacilityData) -> dict[str, object]:
    return {
        **_summary(facility),
        "receipt": facility.receipt,
        "source_urls": facility.source_urls,
        "unknown_summary": facility.unknown_summary,
    }


def _review_payload(review: ReviewRecord) -> dict[str, object]:
    return {
        "review_id": str(review.review_id),
        "record_key": review.record_key,
        "capability": review.capability,
        "decision": review.decision,
        "note": review.note,
        "run_id": review.run_id,
        "system_verdict": review.system_verdict,
        "created_at": review.created_at.isoformat(),
    }


def _reviewer(header: str | None) -> str:
    if header is None or not header.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Reviewer identity unavailable.")
    return header.strip()[:320]


def unavailable(operation: str, error: Exception) -> HTTPException:
    LOGGER.error("Walking skeleton %s failed (%s)", operation, type(error).__name__)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Walking skeleton data is temporarily unavailable.",
    )


def create_app(result_store: ResultStore, review_store: ReviewStore) -> FastAPI:
    """Create the app around replaceable Databricks and Lakebase boundaries."""
    application = FastAPI(title="Facility Trust Desk", docs_url=None, redoc_url=None)

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(Path(__file__).with_name("index.html"))

    @application.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @application.get("/api/options")
    def options() -> dict[str, object]:
        try:
            return result_store.options()
        except Exception as error:
            raise unavailable("options", error) from None

    @application.get("/api/results")
    def results(
        capability: str = Query(min_length=2, max_length=40),
        region: str = Query(min_length=1, max_length=100),
    ) -> dict[str, object]:
        if capability not in CAPABILITIES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown capability.")
        try:
            facilities = result_store.search(capability, region)
        except Exception as error:
            raise unavailable("results", error) from None
        return {"capability": capability, "region": region, "facilities": [_summary(item) for item in facilities]}

    @application.get("/api/receipts/{record_key:path}")
    def receipt(
        record_key: str,
        capability: str = Query(min_length=2, max_length=40),
    ) -> dict[str, object]:
        if capability not in CAPABILITIES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown capability.")
        try:
            facility = result_store.receipt(record_key, capability)
        except Exception as error:
            raise unavailable("receipt", error) from None
        if facility is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found.")
        return _detail(facility)

    @application.post("/api/reviews", status_code=status.HTTP_201_CREATED)
    def save_review(
        request: ReviewRequest,
        x_forwarded_user: str | None = Header(default=None),
    ) -> dict[str, object]:
        reviewer = _reviewer(x_forwarded_user)
        if request.capability not in CAPABILITIES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown capability.")
        note = request.note.strip() if request.note else None
        if request.decision == "overridden" and not note:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Override note required.")
        try:
            facility = result_store.receipt(request.record_key, request.capability)
            if facility is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found.")
            review = review_store.save(
                ReviewRecord(
                    review_id=uuid4(),
                    record_key=request.record_key,
                    capability=request.capability,
                    decision=request.decision,
                    note=note,
                    run_id=facility.run_id,
                    system_verdict=facility.support_tier,
                    reviewer=reviewer,
                    created_at=datetime.now(UTC),
                )
            )
        except HTTPException:
            raise
        except Exception as error:
            raise unavailable("review write", error) from None
        return _review_payload(review)

    @application.get("/api/reviews/{record_key:path}")
    def latest_review(
        record_key: str,
        capability: str = Query(min_length=2, max_length=40),
        x_forwarded_user: str | None = Header(default=None),
    ) -> dict[str, object]:
        reviewer = _reviewer(x_forwarded_user)
        try:
            review = review_store.latest(record_key, capability, reviewer)
        except Exception as error:
            raise unavailable("review read", error) from None
        if review is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found.")
        return _review_payload(review)

    return application


app = create_app(DatabricksResultStore(), LakebaseReviewStore())
