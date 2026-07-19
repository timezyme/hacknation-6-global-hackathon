"""Validated value objects passed across Trust Desk component boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FacilityRecord:
    """One validated facility row, stripped of its raw-table representation."""

    record_key: str
    facility_id: str
    name: str | None
    description: str | None
    capability: tuple[str, ...]
    procedure: tuple[str, ...]
    equipment: tuple[str, ...]
    source_urls: tuple[str, ...]
    region: str | None


@dataclass(frozen=True, order=True)
class Claim:
    """One target capability asserted by one validated facility record."""

    record_key: str
    capability: str


@dataclass(frozen=True)
class QuarantinedRecord:
    """Recoverable identity and claim context for a row rejected at ingest."""

    record_key: str
    facility_id: str | None
    region: str | None
    asserted_capabilities: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class IngestBatch:
    """Deterministic result of validating and deduplicating a raw-row batch."""

    input_count: int
    accepted_input_count: int
    quarantined_input_count: int
    records: tuple[FacilityRecord, ...]
    claims: tuple[Claim, ...]
    quarantined: tuple[QuarantinedRecord, ...]
    duplicate_rows_collapsed: int
