"""Batch-time similar-facility context from a Databricks Vector Search index.

For one claim, this module fetches the most textually similar facility records in the
same dataset. The result is comparison context for a receipt - what listings like this
one look like elsewhere - and is framed as such. Similarity is not verification, so
this never produces a check outcome and never runs while a planner waits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import QueryVectorIndexResponse

from trustdesk.models import FacilityRecord

DEFAULT_INDEX_NAME = "workspace.default.trustdesk_facility_text_index"
DEFAULT_NEIGHBOR_COUNT = 3
QUERY_TEXT_MAX_CHARS = 1000
INDEX_COLUMNS: tuple[str, ...] = ("facility_id", "facility_name", "region")
FRAMING = (
    "Most similar facility records in the same dataset, by text similarity. "
    "Comparison context only - similarity is not verification."
)
NO_TEXT_FRAMING = "No readable text to compare against other facility records."


@dataclass(frozen=True)
class Neighbor:
    """One similar facility row returned by the index."""

    facility_id: str
    facility_name: str | None
    region: str | None
    score: float


@dataclass(frozen=True)
class SimilarContext:
    """Receipt-ready comparison context for one claim."""

    record_key: str
    capability: str
    framing: str
    neighbors: tuple[Neighbor, ...]


class NeighborClient(Protocol):
    """External boundary used by the context builder and in-memory test adapters."""

    def find_neighbors(self, query_text: str, count: int) -> tuple[Neighbor, ...]: ...


class VectorIndexQueryClient(Protocol):
    """Subset of the Databricks vector search API used by the production adapter."""

    def query_index(
        self,
        index_name: str,
        columns: list[str],
        *,
        query_text: str | None = None,
        num_results: int | None = None,
    ) -> QueryVectorIndexResponse: ...


def _record_text(record: FacilityRecord) -> str:
    parts = (record.description or "", *record.capability, *record.equipment, *record.procedure)
    return " ".join(part.strip() for part in parts if part.strip())


def similar_context(
    record: FacilityRecord,
    capability: str,
    client: NeighborClient,
    count: int = DEFAULT_NEIGHBOR_COUNT,
) -> SimilarContext:
    """Build honest comparison context for one claim, excluding the record itself."""
    text = _record_text(record)
    if not text:
        return SimilarContext(record.record_key, capability, NO_TEXT_FRAMING, ())
    query = f"{capability} {text}"[:QUERY_TEXT_MAX_CHARS]
    raw = client.find_neighbors(query, count + 1)
    neighbors = tuple(neighbor for neighbor in raw if neighbor.facility_id != record.facility_id)[:count]
    return SimilarContext(record.record_key, capability, FRAMING, neighbors)


def _optional(value: str) -> str | None:
    return value if value else None


def _parse_response(response: QueryVectorIndexResponse) -> tuple[Neighbor, ...]:
    manifest = response.manifest
    result = response.result
    if manifest is None or manifest.columns is None or result is None:
        raise ValueError("invalid index response")
    names = tuple(column.name for column in manifest.columns)
    required = (*INDEX_COLUMNS, "score")
    if any(name is None for name in names) or any(name not in names for name in required):
        raise ValueError("invalid index response")
    positions = {name: names.index(name) for name in required}
    neighbors: list[Neighbor] = []
    for row in result.data_array or []:
        if len(row) != len(names) or not row[positions["facility_id"]]:
            raise ValueError("invalid index response")
        try:
            score = float(row[positions["score"]])
        except ValueError:
            raise ValueError("invalid index response") from None
        neighbors.append(
            Neighbor(
                facility_id=row[positions["facility_id"]],
                facility_name=_optional(row[positions["facility_name"]]),
                region=_optional(row[positions["region"]]),
                score=score,
            )
        )
    return tuple(neighbors)


class DatabricksNeighborClient:
    """Workspace-authenticated adapter for one vector search index."""

    def __init__(
        self,
        index_name: str = DEFAULT_INDEX_NAME,
        api: VectorIndexQueryClient | None = None,
    ) -> None:
        self.index_name = index_name
        self._api = api

    def _client(self) -> VectorIndexQueryClient:
        if self._api is None:
            self._api = WorkspaceClient().vector_search_indexes
        return self._api

    def find_neighbors(self, query_text: str, count: int) -> tuple[Neighbor, ...]:
        response = self._client().query_index(
            self.index_name,
            list(INDEX_COLUMNS),
            query_text=query_text,
            num_results=count,
        )
        return _parse_response(response)
