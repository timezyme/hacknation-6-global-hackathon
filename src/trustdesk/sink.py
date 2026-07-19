"""Publish complete result batches behind one atomic active-run pointer."""

from __future__ import annotations

import argparse
import base64
import gzip
import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    Disposition,
    Format,
    StatementParameterListItem,
    StatementResponse,
)

from trustdesk.batch import (
    BatchCounts,
    RefereeCallback,
    ResultBatch,
    build_result_batch,
)
from trustdesk.ingest import (
    DEFAULT_PROFILE,
    SOURCE_TABLE,
    _warehouse_id,
    ingest_rows,
    load_live_rows,
)
from trustdesk.ladder import CheckAttempt, EvidenceCoordinate, load_checks
from trustdesk.lexicon import CAPABILITIES
from trustdesk.marks import Verdict

FACILITIES_TABLE = "workspace.default.trustdesk_facility_index"
VERDICTS_TABLE = "workspace.default.trustdesk_verdicts"
RECEIPTS_TABLE = "workspace.default.trustdesk_receipts"
MANIFEST_TABLE = "workspace.default.trustdesk_batch_manifest"
ACTIVE_RUN_TABLE = "workspace.default.trustdesk_active_run"
WALKING_SKELETON_TABLE = "workspace.default.trustdesk_walking_skeleton"
INSERT_CHUNK_SIZE = 250
RECEIPT_CHUNK_SIZE = 100

CREATE_STATEMENTS = (
    f"""CREATE TABLE IF NOT EXISTS {FACILITIES_TABLE} (
        run_id STRING NOT NULL,
        record_key STRING NOT NULL,
        facility_id STRING,
        facility_name STRING,
        region STRING,
        processing_status STRING NOT NULL,
        asserted_capabilities_json STRING NOT NULL,
        quarantine_reasons_json STRING NOT NULL
    ) USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {VERDICTS_TABLE} (
        run_id STRING NOT NULL,
        record_key STRING NOT NULL,
        facility_id STRING,
        facility_name STRING,
        region STRING,
        capability STRING NOT NULL,
        verdict STRING NOT NULL,
        mark_description STRING,
        mark_capability STRING,
        mark_equipment STRING,
        mark_procedure STRING,
        support_item_count BIGINT NOT NULL,
        deciding_checks_json STRING NOT NULL,
        rank BIGINT,
        computed_at TIMESTAMP NOT NULL
    ) USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {RECEIPTS_TABLE} (
        run_id STRING NOT NULL,
        record_key STRING NOT NULL,
        capability STRING,
        receipt_kind STRING NOT NULL,
        receipt_json STRING NOT NULL,
        computed_at TIMESTAMP NOT NULL
    ) USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {MANIFEST_TABLE} (
        run_id STRING NOT NULL,
        pipeline_version STRING NOT NULL,
        input_table STRING NOT NULL,
        input_table_version BIGINT NOT NULL,
        check_config_hash STRING NOT NULL,
        check_config_json STRING NOT NULL,
        model_mode STRING NOT NULL,
        model_version STRING,
        prompt_version STRING,
        input_count BIGINT NOT NULL,
        duplicate_rows_collapsed BIGINT NOT NULL,
        expected_facilities BIGINT NOT NULL,
        expected_verdicts BIGINT NOT NULL,
        expected_receipts BIGINT NOT NULL,
        actual_facilities BIGINT,
        actual_verdicts BIGINT,
        actual_receipts BIGINT,
        orphaned_verdicts BIGINT,
        status STRING NOT NULL,
        started_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP
    ) USING DELTA""",
    f"""CREATE TABLE IF NOT EXISTS {ACTIVE_RUN_TABLE} (
        pointer_name STRING NOT NULL,
        run_id STRING NOT NULL,
        previous_run_id STRING,
        published_at TIMESTAMP NOT NULL
    ) USING DELTA""",
)


class PublicationStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_COMPLETE = "already_complete"


class PublicationError(RuntimeError):
    """Raised when persisted rows do not reconcile with the manifest."""


def encode_receipt(receipt_json: str) -> str:
    """Keep full receipts while staying below Statement Execution payload limits."""
    compressed = gzip.compress(receipt_json.encode(), compresslevel=9, mtime=0)
    return json.dumps(
        {
            "codec": "gzip+base64+json",
            "payload": base64.b64encode(compressed).decode(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_receipt(stored_json: str) -> str:
    """Restore an encoded receipt for the read adapter."""
    envelope = json.loads(stored_json)
    if not isinstance(envelope, dict) or envelope.get("codec") != "gzip+base64+json":
        raise ValueError("unsupported receipt encoding")
    payload = envelope.get("payload")
    if not isinstance(payload, str):
        raise ValueError("invalid receipt encoding")
    return gzip.decompress(base64.b64decode(payload, validate=True)).decode()


class ResultSink(Protocol):
    def is_complete(self, run_id: str) -> bool: ...

    def active_run_id(self) -> str | None: ...

    def begin(self, batch: ResultBatch) -> None: ...

    def write(self, batch: ResultBatch) -> None: ...

    def counts(self, run_id: str) -> BatchCounts: ...

    def complete(self, batch: ResultBatch, actual: BatchCounts) -> None: ...

    def fail(self, run_id: str) -> None: ...

    def activate(self, run_id: str, published_at: datetime) -> None: ...


def publish_batch(sink: ResultSink, batch: ResultBatch) -> PublicationStatus:
    """Publish only after row counts and receipt linkage reconcile."""
    if sink.is_complete(batch.run_id):
        if sink.active_run_id() != batch.run_id:
            sink.activate(batch.run_id, batch.computed_at)
        return PublicationStatus.ALREADY_COMPLETE

    sink.begin(batch)
    try:
        sink.write(batch)
        actual = sink.counts(batch.run_id)
        if actual != batch.counts:
            raise PublicationError("persisted batch counts did not reconcile")
        sink.complete(batch, actual)
        sink.activate(batch.run_id, batch.computed_at)
        if sink.active_run_id() != batch.run_id:
            raise PublicationError("active pointer did not reference the completed run")
    except Exception:
        with suppress(Exception):
            sink.fail(batch.run_id)
        raise
    return PublicationStatus.PUBLISHED


def _parameter(
    name: str,
    value: str | int | datetime | None,
    parameter_type: str,
) -> StatementParameterListItem:
    if isinstance(value, datetime):
        serialized = value.isoformat()
    elif value is None:
        serialized = None
    else:
        serialized = str(value)
    return StatementParameterListItem(
        name=name,
        type=parameter_type,
        value=serialized,
    )


def _statement_rows(response: StatementResponse) -> tuple[dict[str, Any], ...]:
    state = getattr(response.status, "state", None)
    if state is None or state.value != "SUCCEEDED":
        raise RuntimeError("batch publication statement failed")
    if response.result is None:
        return ()
    if response.manifest is None or response.manifest.schema is None:
        raise RuntimeError("batch publication query returned no schema")
    raw_columns = tuple(
        column.name for column in response.manifest.schema.columns or ()
    )
    if any(column is None for column in raw_columns):
        raise RuntimeError("batch publication query returned unnamed columns")
    columns = tuple(column for column in raw_columns if column is not None)
    return tuple(
        dict(zip(columns, values, strict=True))
        for values in response.result.data_array or ()
    )


class DatabricksSink:
    """Run-scoped Delta writes with a single-statement active-pointer flip."""

    def __init__(
        self,
        workspace: WorkspaceClient,
        warehouse_id: str,
        *,
        chunk_size: int = INSERT_CHUNK_SIZE,
    ) -> None:
        self.workspace = workspace
        self.warehouse_id = warehouse_id
        self.chunk_size = chunk_size
        self._ready = False

    def _query(
        self,
        statement: str,
        parameters: Sequence[StatementParameterListItem] = (),
        *,
        row_limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        response = self.workspace.statement_execution.execute_statement(
            statement,
            self.warehouse_id,
            disposition=Disposition.INLINE,
            format=Format.JSON_ARRAY,
            parameters=list(parameters),
            row_limit=row_limit,
            wait_timeout="50s",
        )
        return _statement_rows(response)

    def _ensure_tables(self) -> None:
        if self._ready:
            return
        for statement in CREATE_STATEMENTS:
            self._query(statement)
        self._ready = True

    def _run_parameter(self, run_id: str) -> StatementParameterListItem:
        return _parameter("run_id", run_id, "STRING")

    def _insert_rows(
        self,
        table: str,
        columns: Sequence[str],
        column_types: Sequence[str],
        rows: Iterable[Sequence[str | int | datetime | None]],
        *,
        chunk_size: int | None = None,
    ) -> None:
        effective_chunk_size = chunk_size or self.chunk_size
        pending: list[Sequence[str | int | datetime | None]] = []
        for row in rows:
            pending.append(row)
            if len(pending) == effective_chunk_size:
                self._insert_chunk(table, columns, column_types, pending)
                pending = []
        if pending:
            self._insert_chunk(table, columns, column_types, pending)

    def _insert_chunk(
        self,
        table: str,
        columns: Sequence[str],
        column_types: Sequence[str],
        rows: Sequence[Sequence[str | int | datetime | None]],
    ) -> None:
        parameters: list[StatementParameterListItem] = []
        groups: list[str] = []
        for row_index, row in enumerate(rows):
            if len(row) != len(columns):
                raise ValueError("insert row does not match its schema")
            names = tuple(
                f"{column}_{row_index}" for column in columns
            )
            groups.append("(" + ",".join(f":{name}" for name in names) + ")")
            parameters.extend(
                _parameter(name, value, parameter_type)
                for name, value, parameter_type in zip(
                    names, row, column_types, strict=True
                )
            )
        statement = (
            f"INSERT INTO {table} ({','.join(columns)}) VALUES "
            + ",".join(groups)
        )
        self._query(statement, parameters)

    def is_complete(self, run_id: str) -> bool:
        self._ensure_tables()
        rows = self._query(
            f"SELECT status FROM {MANIFEST_TABLE} WHERE run_id = :run_id LIMIT 1",
            (self._run_parameter(run_id),),
        )
        return bool(rows and rows[0].get("status") == "complete")

    def active_run_id(self) -> str | None:
        self._ensure_tables()
        rows = self._query(
            f"SELECT run_id FROM {ACTIVE_RUN_TABLE} WHERE pointer_name = 'default' LIMIT 1"
        )
        value = rows[0].get("run_id") if rows else None
        return value if isinstance(value, str) else None

    def begin(self, batch: ResultBatch) -> None:
        self._ensure_tables()
        run_parameter = (self._run_parameter(batch.run_id),)
        for table in (FACILITIES_TABLE, VERDICTS_TABLE, RECEIPTS_TABLE, MANIFEST_TABLE):
            self._query(f"DELETE FROM {table} WHERE run_id = :run_id", run_parameter)
        counts = batch.counts
        columns = (
            "run_id",
            "pipeline_version",
            "input_table",
            "input_table_version",
            "check_config_hash",
            "check_config_json",
            "model_mode",
            "model_version",
            "prompt_version",
            "input_count",
            "duplicate_rows_collapsed",
            "expected_facilities",
            "expected_verdicts",
            "expected_receipts",
            "actual_facilities",
            "actual_verdicts",
            "actual_receipts",
            "orphaned_verdicts",
            "status",
            "started_at",
            "completed_at",
        )
        self._insert_rows(
            MANIFEST_TABLE,
            columns,
            (
                "STRING", "STRING", "STRING", "BIGINT", "STRING", "STRING",
                "STRING", "STRING", "STRING", "BIGINT", "BIGINT", "BIGINT",
                "BIGINT", "BIGINT", "BIGINT", "BIGINT", "BIGINT", "BIGINT",
                "STRING", "TIMESTAMP", "TIMESTAMP",
            ),
            ((
                batch.run_id,
                batch.pipeline_version,
                batch.input_table,
                batch.input_table_version,
                batch.check_config_hash,
                batch.check_config_json,
                batch.model_mode,
                batch.model_version,
                batch.prompt_version,
                batch.input_count,
                batch.duplicate_rows_collapsed,
                counts.facilities,
                counts.verdicts,
                counts.receipts,
                None,
                None,
                None,
                None,
                "writing",
                batch.computed_at,
                None,
            ),),
        )

    def write(self, batch: ResultBatch) -> None:
        self._insert_rows(
            FACILITIES_TABLE,
            (
                "run_id", "record_key", "facility_id", "facility_name", "region",
                "processing_status", "asserted_capabilities_json", "quarantine_reasons_json",
            ),
            ("STRING",) * 8,
            (
                (
                    batch.run_id,
                    row.record_key,
                    row.facility_id,
                    row.facility_name,
                    row.region,
                    row.processing_status,
                    json.dumps(row.asserted_capabilities, separators=(",", ":")),
                    json.dumps(row.quarantine_reasons, separators=(",", ":")),
                )
                for row in batch.facilities
            ),
        )
        self._insert_rows(
            VERDICTS_TABLE,
            (
                "run_id", "record_key", "facility_id", "facility_name", "region",
                "capability", "verdict", "mark_description", "mark_capability",
                "mark_equipment", "mark_procedure", "support_item_count",
                "deciding_checks_json", "rank", "computed_at",
            ),
            (
                "STRING", "STRING", "STRING", "STRING", "STRING", "STRING",
                "STRING", "STRING", "STRING", "STRING", "STRING", "BIGINT",
                "STRING", "BIGINT", "TIMESTAMP",
            ),
            (
                (
                    batch.run_id,
                    row.record_key,
                    row.facility_id,
                    row.facility_name,
                    row.region,
                    row.capability,
                    row.verdict.value,
                    *(
                        mark.value if (mark := row.marks[field_name]) is not None else None
                        for field_name in (
                            "description",
                            "capability",
                            "equipment",
                            "procedure",
                        )
                    ),
                    row.support_item_count,
                    json.dumps(row.deciding_checks, separators=(",", ":")),
                    row.rank,
                    row.computed_at,
                )
                for row in batch.verdicts
            ),
        )
        self._insert_rows(
            RECEIPTS_TABLE,
            ("run_id", "record_key", "capability", "receipt_kind", "receipt_json", "computed_at"),
            ("STRING", "STRING", "STRING", "STRING", "STRING", "TIMESTAMP"),
            (
                (
                    batch.run_id,
                    row.record_key,
                    row.capability,
                    row.receipt_kind,
                    encode_receipt(row.receipt_json),
                    row.computed_at,
                )
                for row in batch.receipts
            ),
            chunk_size=RECEIPT_CHUNK_SIZE,
        )

    def counts(self, run_id: str) -> BatchCounts:
        parameter = (self._run_parameter(run_id),)
        rows = self._query(
            f"""SELECT
                (SELECT COUNT(*) FROM {FACILITIES_TABLE} WHERE run_id = :run_id) AS facilities,
                (SELECT COUNT(*) FROM {VERDICTS_TABLE} WHERE run_id = :run_id) AS verdicts,
                (SELECT COUNT(*) FROM {RECEIPTS_TABLE} WHERE run_id = :run_id) AS receipts,
                (SELECT COUNT(*) FROM {VERDICTS_TABLE} v
                 LEFT JOIN {RECEIPTS_TABLE} r
                   ON v.run_id = r.run_id AND v.record_key = r.record_key
                  AND v.capability = r.capability
                 WHERE v.run_id = :run_id AND r.run_id IS NULL) AS orphaned_verdicts""",
            parameter,
        )
        if len(rows) != 1:
            raise RuntimeError("batch count query returned no result")
        row = rows[0]
        return BatchCounts(
            facilities=int(row["facilities"]),
            verdicts=int(row["verdicts"]),
            receipts=int(row["receipts"]),
            orphaned_verdicts=int(row["orphaned_verdicts"]),
        )

    def complete(self, batch: ResultBatch, actual: BatchCounts) -> None:
        parameters = (
            self._run_parameter(batch.run_id),
            _parameter("facilities", actual.facilities, "BIGINT"),
            _parameter("verdicts", actual.verdicts, "BIGINT"),
            _parameter("receipts", actual.receipts, "BIGINT"),
            _parameter("orphaned", actual.orphaned_verdicts, "BIGINT"),
            _parameter("completed_at", datetime.now(UTC), "TIMESTAMP"),
        )
        self._query(
            f"""UPDATE {MANIFEST_TABLE}
                SET actual_facilities = :facilities,
                    actual_verdicts = :verdicts,
                    actual_receipts = :receipts,
                    orphaned_verdicts = :orphaned,
                    status = 'complete',
                    completed_at = :completed_at
                WHERE run_id = :run_id AND status = 'writing'""",
            parameters,
        )

    def fail(self, run_id: str) -> None:
        self._query(
            f"""UPDATE {MANIFEST_TABLE}
                SET status = 'failed', completed_at = :completed_at
                WHERE run_id = :run_id AND status <> 'complete'""",
            (
                self._run_parameter(run_id),
                _parameter("completed_at", datetime.now(UTC), "TIMESTAMP"),
            ),
        )

    def activate(self, run_id: str, published_at: datetime) -> None:
        previous = self.active_run_id()
        if previous == run_id:
            return
        self._query(
            f"""MERGE INTO {ACTIVE_RUN_TABLE} AS target
                USING (
                    SELECT 'default' AS pointer_name,
                           :run_id AS run_id,
                           :previous_run_id AS previous_run_id,
                           :published_at AS published_at
                    WHERE EXISTS (
                        SELECT 1 FROM {MANIFEST_TABLE}
                        WHERE run_id = :run_id AND status = 'complete'
                    )
                ) AS source
                ON target.pointer_name = source.pointer_name
                WHEN MATCHED THEN UPDATE SET
                    run_id = source.run_id,
                    previous_run_id = source.previous_run_id,
                    published_at = source.published_at
                WHEN NOT MATCHED THEN INSERT *""",
            (
                self._run_parameter(run_id),
                _parameter("previous_run_id", previous, "STRING"),
                _parameter("published_at", published_at, "TIMESTAMP"),
            ),
        )

    def smoke_summary(self, run_id: str) -> dict[str, object]:
        rows = self._query(
            f"""SELECT capability, verdict, COUNT(*) AS row_count
                FROM {VERDICTS_TABLE}
                WHERE run_id = :run_id
                GROUP BY capability, verdict
                ORDER BY capability, verdict""",
            (self._run_parameter(run_id),),
            row_limit=100,
        )
        observed_capabilities = {row.get("capability") for row in rows}
        observed_verdicts = {row.get("verdict") for row in rows}
        allowed_verdicts = {verdict.value for verdict in Verdict}
        skeleton = self._query(
            f"SELECT COUNT(*) AS row_count FROM {WALKING_SKELETON_TABLE}"
        )
        return {
            "active_run_matches": self.active_run_id() == run_id,
            "all_six_capabilities": observed_capabilities == set(CAPABILITIES),
            "allowed_verdicts_only": observed_verdicts <= allowed_verdicts,
            "counts": self.counts(run_id).__dict__,
            "observed_verdict_distribution": [
                {
                    "capability": row.get("capability"),
                    "verdict": row.get("verdict"),
                    "count": int(row["row_count"]),
                }
                for row in rows
            ],
            "walking_skeleton_rows_preserved": int(skeleton[0]["row_count"]),
        }


def source_table_version(
    workspace: WorkspaceClient,
    warehouse_id: str,
    table: str = SOURCE_TABLE,
) -> int:
    response = workspace.statement_execution.execute_statement(
        f"DESCRIBE HISTORY {table}",
        warehouse_id,
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
        row_limit=100,
        wait_timeout="50s",
    )
    rows = _statement_rows(response)
    if not rows or "version" not in rows[0]:
        raise RuntimeError("source table version was unavailable")
    return int(rows[0]["version"])


def _configured_referee() -> tuple[
    RefereeCallback | None,
    Mapping[str, object],
]:
    try:
        from trustdesk.referee import (
            REFEREE_VERSION,
            Referee,
            load_referee_config,
        )
    except ImportError:
        return None, {"enabled": False, "mode": "none", "version": "none"}

    config = load_referee_config()
    metadata = {
        "enabled": config.enabled,
        "max_model_bundles": config.max_model_bundles,
        "mode": config.mode,
        "version": REFEREE_VERSION,
    }
    if not config.enabled:
        return None, metadata
    configured = Referee(config)

    def referee(
        capability: str,
        decisions: tuple[CheckAttempt, ...],
    ) -> dict[EvidenceCoordinate, dict[str, str]]:
        findings = configured.referee_claim(capability, decisions)
        return {
            finding.coordinate: {
                "method": finding.method,
                "outcome": finding.outcome.value,
                "rationale": finding.rationale,
                "version": finding.referee_version,
            }
            for finding in findings
        }

    return referee, metadata


def run_live(profile: str = DEFAULT_PROFILE) -> dict[str, object]:
    workspace = WorkspaceClient(profile=profile)
    warehouse_id = _warehouse_id(workspace)
    version_before = source_table_version(workspace, warehouse_id)
    rows = load_live_rows(workspace, warehouse_id)
    version_after = source_table_version(workspace, warehouse_id)
    if version_before != version_after:
        raise RuntimeError("source table changed during batch read")
    referee, referee_config = _configured_referee()
    batch = build_result_batch(
        ingest_rows(rows),
        load_checks(),
        input_table_version=version_before,
        referee=referee,
        referee_config=referee_config,
    )
    sink = DatabricksSink(workspace, warehouse_id)
    status = publish_batch(sink, batch)
    return {
        "publication_status": status.value,
        "run_id": batch.run_id,
        "manifest": sink.smoke_summary(batch.run_id),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and publish the complete Trust Desk batch")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    args = parser.parse_args()
    try:
        summary = run_live(args.profile)
    except Exception as error:
        print(json.dumps({"status": "fail", "error_type": type(error).__name__}))
        return 1
    print(json.dumps({"status": "pass", **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
