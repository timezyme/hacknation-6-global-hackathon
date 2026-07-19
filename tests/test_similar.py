"""Contract tests for the similar-facility vector context module."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from databricks.sdk.service.vectorsearch import (
    ColumnInfo,
    QueryVectorIndexResponse,
    ResultData,
    ResultManifest,
)

from trustdesk.models import FacilityRecord
from trustdesk.similar import (
    FRAMING,
    NO_TEXT_FRAMING,
    QUERY_TEXT_MAX_CHARS,
    DatabricksNeighborClient,
    Neighbor,
    similar_context,
)


def _record(
    record_key: str = "rec-1",
    facility_id: str = "fac-1",
    description: str | None = "District hospital with a six bed intensive care unit.",
    capability: tuple[str, ...] = ("ICU", "maternity"),
    equipment: tuple[str, ...] = ("ventilator",),
    procedure: tuple[str, ...] = (),
) -> FacilityRecord:
    return FacilityRecord(
        record_key=record_key,
        facility_id=facility_id,
        name="Test Hospital",
        description=description,
        capability=capability,
        procedure=procedure,
        equipment=equipment,
        source_urls=(),
        region="Kerala",
    )


def _neighbor(facility_id: str, score: float) -> Neighbor:
    return Neighbor(facility_id=facility_id, facility_name=f"Hospital {facility_id}", region="Kerala", score=score)


@dataclass(frozen=True)
class InMemoryNeighborClient:
    """Deterministic in-memory adapter used by tests."""

    neighbors: tuple[Neighbor, ...]

    def find_neighbors(self, query_text: str, count: int) -> tuple[Neighbor, ...]:
        return self.neighbors[:count]


class RecordingNeighborClient:
    """Captures the query without returning neighbors."""

    def __init__(self) -> None:
        self.queries: tuple[tuple[str, int], ...] = ()

    def find_neighbors(self, query_text: str, count: int) -> tuple[Neighbor, ...]:
        self.queries = (*self.queries, (query_text, count))
        return ()


class ExplodingNeighborClient:
    """Fails the test if any query reaches the index."""

    def find_neighbors(self, query_text: str, count: int) -> tuple[Neighbor, ...]:
        raise AssertionError("no query expected for a record without text")


class FakeIndexApi:
    """Fake of the Databricks vector search query API."""

    def __init__(self, response: QueryVectorIndexResponse) -> None:
        self.response = response
        self.calls: tuple[tuple[str, tuple[str, ...], str | None, int | None], ...] = ()

    def query_index(
        self,
        index_name: str,
        columns: list[str],
        *,
        query_text: str | None = None,
        num_results: int | None = None,
    ) -> QueryVectorIndexResponse:
        self.calls = (*self.calls, (index_name, tuple(columns), query_text, num_results))
        return self.response


_ALL_COLUMNS = ("facility_id", "facility_name", "region", "score")


def _response(rows: list[list[str]], names: tuple[str, ...] = _ALL_COLUMNS) -> QueryVectorIndexResponse:
    return QueryVectorIndexResponse(
        manifest=ResultManifest(column_count=len(names), columns=[ColumnInfo(name=name) for name in names]),
        result=ResultData(data_array=rows, row_count=len(rows)),
    )


def test_record_without_text_returns_empty_context_without_querying() -> None:
    record = _record(description=None, capability=(), equipment=(), procedure=())
    context = similar_context(record, "ICU", ExplodingNeighborClient())
    assert context.record_key == "rec-1"
    assert context.capability == "ICU"
    assert context.framing == NO_TEXT_FRAMING
    assert context.neighbors == ()


def test_query_prepends_capability_requests_one_extra_and_truncates() -> None:
    client = RecordingNeighborClient()
    record = _record(description="x" * (2 * QUERY_TEXT_MAX_CHARS))
    similar_context(record, "ICU", client, count=3)
    ((query, count),) = client.queries
    assert query.startswith("ICU ")
    assert len(query) == QUERY_TEXT_MAX_CHARS
    assert count == 4


def test_excludes_own_facility_and_caps_count() -> None:
    neighbors = (
        _neighbor("fac-9", 0.9),
        _neighbor("fac-1", 0.8),
        _neighbor("fac-8", 0.7),
        _neighbor("fac-7", 0.6),
    )
    context = similar_context(_record(), "ICU", InMemoryNeighborClient(neighbors), count=3)
    assert context.framing == FRAMING
    assert tuple(neighbor.facility_id for neighbor in context.neighbors) == ("fac-9", "fac-8", "fac-7")


def test_caps_count_when_own_facility_is_absent() -> None:
    neighbors = tuple(_neighbor(f"fac-{i}", 1.0 - i / 10) for i in range(2, 7))
    context = similar_context(_record(), "ICU", InMemoryNeighborClient(neighbors), count=3)
    assert len(context.neighbors) == 3


def test_databricks_adapter_parses_neighbors_in_order() -> None:
    api = FakeIndexApi(
        _response(
            [
                ["fac-9", "Ninth Hospital", "Kerala", "0.91"],
                ["fac-8", "", "", "0.72"],
            ]
        )
    )
    client = DatabricksNeighborClient(index_name="cat.sch.idx", api=api)
    neighbors = client.find_neighbors("icu ventilator", 2)
    ((index_name, columns, query, count),) = api.calls
    assert index_name == "cat.sch.idx"
    assert columns == ("facility_id", "facility_name", "region")
    assert query == "icu ventilator"
    assert count == 2
    assert neighbors == (
        Neighbor(facility_id="fac-9", facility_name="Ninth Hospital", region="Kerala", score=0.91),
        Neighbor(facility_id="fac-8", facility_name=None, region=None, score=0.72),
    )


@pytest.mark.parametrize(
    "response",
    [
        QueryVectorIndexResponse(manifest=None, result=ResultData(data_array=[], row_count=0)),
        _response([["fac-9", "Ninth", "Kerala", "0.9"]], names=("facility_id", "facility_name", "region")),
        _response([["fac-9", "Ninth", "0.9"]]),
        _response([["", "Ninth", "Kerala", "0.9"]]),
        _response([["fac-9", "Ninth", "Kerala", "not-a-score"]]),
    ],
)
def test_databricks_adapter_rejects_malformed_responses(response: QueryVectorIndexResponse) -> None:
    client = DatabricksNeighborClient(index_name="cat.sch.idx", api=FakeIndexApi(response))
    with pytest.raises(ValueError, match="invalid index response"):
        client.find_neighbors("icu", 1)


@pytest.mark.parametrize(
    "make_client",
    [
        lambda neighbors: InMemoryNeighborClient(neighbors),
        lambda neighbors: DatabricksNeighborClient(
            index_name="cat.sch.idx",
            api=FakeIndexApi(
                _response(
                    [[n.facility_id, n.facility_name or "", n.region or "", str(n.score)] for n in neighbors]
                )
            ),
        ),
    ],
    ids=["in_memory", "databricks"],
)
def test_both_adapters_satisfy_the_same_context_contract(make_client: object) -> None:
    neighbors = (_neighbor("fac-9", 0.9), _neighbor("fac-1", 0.8), _neighbor("fac-8", 0.7))
    context = similar_context(_record(), "ICU", make_client(neighbors), count=2)  # type: ignore[operator]
    assert context.framing == FRAMING
    assert tuple(neighbor.facility_id for neighbor in context.neighbors) == ("fac-9", "fac-8")
    assert all(neighbor.facility_id != "fac-1" for neighbor in context.neighbors)
