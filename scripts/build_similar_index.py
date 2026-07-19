"""Provision the facility-text vector index and prove one similarity query.

Idempotent steps, each skipped when already done:
1. Build workspace.default.trustdesk_facility_text - one deduplicated text row per
   facility id, change data feed enabled, no raw rows leave the workspace.
2. Create a STANDARD vector search endpoint and wait for ONLINE.
3. Create a TRIGGERED delta-sync index with managed gte-large embeddings and wait
   until it is ready.
4. Run one smoke query through the production adapter in src/trustdesk/similar.py.
5. Write a sanitized aggregate artifact to artifacts/vector-index-proof.json.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors.platform import NotFound
from databricks.sdk.service.sql import Disposition, Format, StatementState
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    EndpointStatusState,
    EndpointType,
    PipelineType,
    VectorIndexType,
)

from trustdesk.ingest import SOURCE_TABLE
from trustdesk.similar import DatabricksNeighborClient

DEFAULT_ENDPOINT_NAME = "trustdesk-vector"
DEFAULT_SOURCE_TABLE = "workspace.default.trustdesk_facility_text"
DEFAULT_INDEX_NAME = "workspace.default.trustdesk_facility_text_index"
DEFAULT_EMBEDDING_ENDPOINT = "databricks-gte-large-en"
DEFAULT_ARTIFACT_PATH = Path("artifacts/vector-index-proof.json")
SMOKE_QUERY = "ICU intensive care unit ventilator critical care beds"
POLL_SECONDS = 20
ENDPOINT_TIMEOUT_SECONDS = 1500
INDEX_TIMEOUT_SECONDS = 2100

TEXT_TABLE_SQL = """
CREATE OR REPLACE TABLE {table}
TBLPROPERTIES (delta.enableChangeDataFeed = true) AS
WITH prepared AS (
    SELECT
        unique_id AS facility_id,
        name AS facility_name,
        address_stateOrRegion AS region,
        concat_ws(' ', coalesce(description, ''), coalesce(capability, ''),
                  coalesce(equipment, ''), coalesce(procedure, '')) AS text
    FROM {source}
    WHERE unique_id IS NOT NULL
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY facility_id ORDER BY sha2(text, 256)) AS rn
    FROM prepared
    WHERE length(trim(text)) > 0
)
SELECT facility_id, facility_name, region, text FROM ranked WHERE rn = 1{limit}
"""
# When capped, keep only rows relevant to the six target capabilities, richest text
# first, so a small index still returns sensible neighbors for every demo claim.
LIMIT_FILTER_SQL = """ AND text rlike
    '(?i)(icu|intensive care|critical care|maternity|obstetric|emergency|casualty|oncology|cancer|trauma|nicu|neonatal)'
ORDER BY length(text) DESC, facility_id LIMIT {limit}"""


def _warehouse_id(workspace: WorkspaceClient) -> str:
    warehouses = tuple(workspace.warehouses.list())
    if len(warehouses) != 1 or not warehouses[0].id:
        raise RuntimeError("expected exactly one SQL warehouse")
    return warehouses[0].id


def _execute(workspace: WorkspaceClient, warehouse_id: str, statement: str) -> tuple[list[str], ...]:
    response = workspace.statement_execution.execute_statement(
        statement,
        warehouse_id,
        format=Format.JSON_ARRAY,
        disposition=Disposition.INLINE,
        wait_timeout="50s",
    )
    state = response.status.state if response.status else None
    if state is not StatementState.SUCCEEDED:
        raise RuntimeError(f"statement failed with state {state}")
    if response.result is None or response.result.data_array is None:
        return ()
    return tuple(response.result.data_array)


def _build_text_table(workspace: WorkspaceClient, table: str, limit: int | None) -> int:
    warehouse_id = _warehouse_id(workspace)
    limit_sql = LIMIT_FILTER_SQL.format(limit=int(limit)) if limit is not None else ""
    statement = TEXT_TABLE_SQL.format(table=table, source=SOURCE_TABLE, limit=limit_sql)
    _execute(workspace, warehouse_id, statement)
    rows = _execute(workspace, warehouse_id, f"SELECT count(*) FROM {table}")
    return int(rows[0][0])


def _ensure_endpoint(workspace: WorkspaceClient, name: str) -> str:
    existing = {endpoint.name for endpoint in workspace.vector_search_endpoints.list_endpoints()}
    if name not in existing:
        print(f"creating vector search endpoint {name}", flush=True)
        workspace.vector_search_endpoints.create_endpoint(name=name, endpoint_type=EndpointType.STANDARD)
    deadline = time.monotonic() + ENDPOINT_TIMEOUT_SECONDS
    while True:
        endpoint = workspace.vector_search_endpoints.get_endpoint(name)
        state = endpoint.endpoint_status.state if endpoint.endpoint_status else None
        print(f"endpoint state: {state}", flush=True)
        if state is EndpointStatusState.ONLINE:
            return str(state)
        if state is EndpointStatusState.OFFLINE:
            raise RuntimeError("vector search endpoint went offline")
        if time.monotonic() > deadline:
            raise RuntimeError(f"endpoint not online after {ENDPOINT_TIMEOUT_SECONDS}s (state {state})")
        time.sleep(POLL_SECONDS)


def _ensure_index(
    workspace: WorkspaceClient,
    endpoint_name: str,
    index_name: str,
    source_table: str,
    embedding_endpoint: str,
) -> dict[str, Any]:
    try:
        workspace.vector_search_indexes.get_index(index_name)
    except NotFound:
        print(f"creating delta-sync index {index_name}", flush=True)
        workspace.vector_search_indexes.create_index(
            name=index_name,
            endpoint_name=endpoint_name,
            primary_key="facility_id",
            index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=source_table,
                pipeline_type=PipelineType.TRIGGERED,
                embedding_source_columns=[
                    EmbeddingSourceColumn(
                        name="text",
                        embedding_model_endpoint_name=embedding_endpoint,
                    )
                ],
            ),
        )
    deadline = time.monotonic() + INDEX_TIMEOUT_SECONDS
    while True:
        index = workspace.vector_search_indexes.get_index(index_name)
        status = index.status
        ready = bool(status.ready) if status else False
        indexed = int(status.indexed_row_count or 0) if status else 0
        print(f"index ready: {ready}, indexed rows: {indexed}", flush=True)
        if ready and indexed > 0:
            return {"ready": True, "indexed_row_count": indexed}
        if time.monotonic() > deadline:
            return {"ready": ready, "indexed_row_count": indexed, "timed_out_after_seconds": INDEX_TIMEOUT_SECONDS}
        time.sleep(POLL_SECONDS)


def _smoke_query(index_name: str, api: WorkspaceClient) -> dict[str, Any]:
    client = DatabricksNeighborClient(index_name=index_name, api=api.vector_search_indexes)
    neighbors = client.find_neighbors(SMOKE_QUERY, 3)
    return {
        "query_terms": SMOKE_QUERY,
        "neighbor_count": len(neighbors),
        "top_score": neighbors[0].score if neighbors else None,
        "regions_returned": len({neighbor.region for neighbor in neighbors if neighbor.region}),
        "passed": len(neighbors) > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="trustdesk-spike")
    parser.add_argument("--endpoint-name", default=DEFAULT_ENDPOINT_NAME)
    parser.add_argument("--source-table", default=DEFAULT_SOURCE_TABLE)
    parser.add_argument("--index-name", default=DEFAULT_INDEX_NAME)
    parser.add_argument("--embedding-endpoint", default=DEFAULT_EMBEDDING_ENDPOINT)
    parser.add_argument("--limit", type=int, default=None, help="cap source rows for a bounded run")
    parser.add_argument("--skip-table", action="store_true", help="reuse the existing text table")
    args = parser.parse_args()

    workspace = WorkspaceClient(profile=args.profile)
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "endpoint_name": args.endpoint_name,
        "index_name": args.index_name,
        "source_table": args.source_table,
        "embedding_endpoint": args.embedding_endpoint,
        "framing": "similarity context only, not verification",
    }
    try:
        if args.skip_table:
            rows = _execute(workspace, _warehouse_id(workspace), f"SELECT count(*) FROM {args.source_table}")
            artifact["source_row_count"] = int(rows[0][0])
        else:
            artifact["source_row_count"] = _build_text_table(workspace, args.source_table, args.limit)
        print(f"text table rows: {artifact['source_row_count']}", flush=True)
        artifact["endpoint_state"] = _ensure_endpoint(workspace, args.endpoint_name)
        artifact["index"] = _ensure_index(
            workspace, args.endpoint_name, args.index_name, args.source_table, args.embedding_endpoint
        )
        if artifact["index"].get("ready"):
            artifact["smoke"] = _smoke_query(args.index_name, workspace)
        artifact["status"] = "pass" if artifact.get("smoke", {}).get("passed") else "incomplete"
    except Exception as error:  # the artifact must record the failure class
        artifact["status"] = "fail"
        artifact["failure_class"] = type(error).__name__
        print(f"failed: {type(error).__name__}", flush=True)
    DEFAULT_ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(f"artifact written: {DEFAULT_ARTIFACT_PATH} status={artifact['status']}", flush=True)
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
