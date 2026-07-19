"""Validate raw facility rows and emit clean value objects."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, ResultData

from trustdesk.models import Claim, FacilityRecord, IngestBatch, QuarantinedRecord

SOURCE_TABLE = "virtue_foundation_dais_2026.virtue_foundation_dataset.facilities"
SOURCE_QUERY = f"""
SELECT
    unique_id,
    name,
    description,
    capability,
    procedure,
    equipment,
    source_urls,
    address_stateOrRegion,
    address_country,
    address_countryCode,
    sha2(to_json(struct(*)), 256) AS _row_fingerprint
FROM {SOURCE_TABLE}
"""
DEFAULT_PROFILE = "trustdesk-spike"
DEFAULT_AUDIT_PATH = Path("artifacts/ingest-audit.json")
ASSERTION_ALIASES: dict[str, tuple[str, ...]] = {
    "ICU": ("icu", "intensive care", "critical care unit"),
    "maternity": ("maternity", "obstetric", "obstetrics", "gynaecology", "gynecology"),
    "emergency": ("emergency", "casualty"),
    "oncology": ("oncology", "cancer care", "cancer treatment"),
    "trauma": ("trauma", "trauma centre", "trauma center"),
    "NICU": ("nicu", "neonatal intensive care"),
}
ASSERTION_PATTERNS = {
    capability: re.compile(
        r"\b(?:" + "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )
    for capability, aliases in ASSERTION_ALIASES.items()
}
OFFICIAL_REGIONS = (
    "Andaman and Nicobar Islands",
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chandigarh",
    "Chhattisgarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jammu and Kashmir",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Ladakh",
    "Lakshadweep",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Puducherry",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
)
REGION_ALIASES = {
    "jammu & kashmir": "Jammu and Kashmir",
    "nct of delhi": "Delhi",
    "orissa": "Odisha",
    "tamilnadu": "Tamil Nadu",
    "up": "Uttar Pradesh",
}
REGIONS_BY_KEY = {region.casefold(): region for region in OFFICIAL_REGIONS} | REGION_ALIASES


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("not text")
    value = value.strip()
    return value or None


def _items(value: object) -> tuple[str, ...]:
    text = _text(value)
    if text is None or text.lower() == "null":
        return ()
    parsed = json.loads(text)
    if not isinstance(parsed, list) or any(item is not None and not isinstance(item, str) for item in parsed):
        raise ValueError("not a string array")
    return tuple(item for raw in parsed if isinstance(raw, str) and (item := raw.strip()))


def _facility_id(value: object) -> str:
    text = _text(value)
    if text is None or len(text) > 512 or not text.isprintable():
        raise ValueError("missing identifier")
    return text


def _region(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return REGIONS_BY_KEY.get(" ".join(text.split()).casefold())


def _asserted_capabilities(items: tuple[str, ...]) -> tuple[str, ...]:
    asserted: set[str] = set()
    for item in items:
        nicu_spans = [match.span() for match in ASSERTION_PATTERNS["NICU"].finditer(item)]
        if nicu_spans:
            asserted.add("NICU")
        for capability, pattern in ASSERTION_PATTERNS.items():
            if capability == "NICU":
                continue
            matches = pattern.finditer(item)
            if capability == "ICU":
                matches = (
                    match
                    for match in matches
                    if not any(start <= match.start() and match.end() <= end for start, end in nicu_spans)
                )
            if next(matches, None) is not None:
                asserted.add(capability)
    return tuple(capability for capability in ASSERTION_ALIASES if capability in asserted)


def _claims(record: FacilityRecord) -> tuple[Claim, ...]:
    return tuple(Claim(record.record_key, capability) for capability in _asserted_capabilities(record.capability))


def _row_fingerprint(row: Mapping[str, Any]) -> str:
    supplied = row.get("_row_fingerprint")
    if isinstance(supplied, str) and len(supplied) == 64:
        try:
            bytes.fromhex(supplied)
        except ValueError:
            pass
        else:
            return supplied.lower()
    serialized = json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(serialized.encode()).hexdigest()


def ingest_rows(rows: Iterable[Mapping[str, Any]]) -> IngestBatch:
    """Validate synthetic or live raw rows through the ingest boundary."""
    rows = tuple(rows)
    parsed: list[tuple[FacilityRecord, str]] = []
    quarantined: list[QuarantinedRecord] = []
    for row in rows:
        fingerprint = _row_fingerprint(row)
        reasons: set[str] = set()
        try:
            facility_id = _facility_id(row.get("unique_id"))
        except (TypeError, ValueError):
            facility_id = None
            reasons.add("invalid_identifier")

        try:
            name = _text(row.get("name"))
            region = _region(row.get("address_stateOrRegion"))
            description = _text(row.get("description"))
        except ValueError:
            name = None
            region = None
            description = None
            reasons.add("invalid_text")

        parsed_items: dict[str, tuple[str, ...]] = {}
        for field in ("capability", "procedure", "equipment", "source_urls"):
            try:
                parsed_items[field] = _items(row.get(field))
            except (json.JSONDecodeError, ValueError):
                parsed_items[field] = ()
                reasons.add("invalid_array")
        capability = parsed_items["capability"]
        procedure = parsed_items["procedure"]
        equipment = parsed_items["equipment"]
        source_urls = parsed_items["source_urls"]

        identity_hash = sha256(facility_id.encode()).hexdigest() if facility_id is not None else None
        record_key = f"record:{identity_hash}:{fingerprint}" if identity_hash else f"quarantine:{fingerprint}"

        if reasons:
            quarantined.append(
                QuarantinedRecord(
                    record_key=record_key,
                    facility_id=facility_id,
                    region=region,
                    asserted_capabilities=_asserted_capabilities(capability),
                    reasons=tuple(sorted(reasons)),
                )
            )
            continue

        assert facility_id is not None
        record = FacilityRecord(
            record_key=record_key,
            facility_id=facility_id,
            name=name,
            description=description,
            capability=capability,
            procedure=procedure,
            equipment=equipment,
            source_urls=source_urls,
            region=region,
        )
        parsed.append((record, fingerprint))

    records_by_key = {record.record_key: record for record, _ in parsed}
    quarantined_by_key = {record.record_key: record for record in quarantined}
    records = tuple(records_by_key[key] for key in sorted(records_by_key))
    quarantined_records = tuple(quarantined_by_key[key] for key in sorted(quarantined_by_key))
    duplicate_rows_collapsed = len(rows) - len(records) - len(quarantined_records)
    claims = tuple(claim for record in records for claim in _claims(record)) + tuple(
        Claim(record.record_key, capability)
        for record in quarantined_records
        for capability in record.asserted_capabilities
    )
    return IngestBatch(
        input_count=len(rows),
        accepted_input_count=len(parsed),
        quarantined_input_count=len(quarantined),
        records=records,
        claims=claims,
        quarantined=quarantined_records,
        duplicate_rows_collapsed=duplicate_rows_collapsed,
    )


def build_audit(batch: IngestBatch) -> dict[str, Any]:
    """Build the aggregate-only evidence artifact for an ingest batch."""
    reason_counts = Counter(reason for record in batch.quarantined for reason in record.reasons)
    claim_counts = Counter(claim.capability for claim in batch.claims)
    indexed_record_count = len(batch.records) + len(batch.quarantined)
    record_key_values = tuple(record.record_key for record in batch.records) + tuple(
        record.record_key for record in batch.quarantined
    )
    record_keys = set(record_key_values)
    claim_values = tuple(batch.claims)
    accepted_input_rows = batch.accepted_input_count
    validation = {
        "claim_keys_known": all(claim.record_key in record_keys for claim in claim_values),
        "claims_unique": len(set(claim_values)) == len(claim_values),
        "duplicate_count_reconciled": (
            batch.duplicate_rows_collapsed == batch.input_count - indexed_record_count
        ),
        "input_count_reconciled": (
            batch.input_count == batch.accepted_input_count + batch.quarantined_input_count
        ),
        "record_keys_unique": len(record_keys) == len(record_key_values),
    }
    resolved_regions = sum(record.region is not None for record in batch.records) + sum(
        record.region is not None for record in batch.quarantined
    )

    return {
        "schema_version": 1,
        "status": "pass" if all(validation.values()) else "fail",
        "source": {"input_rows": batch.input_count, "table": SOURCE_TABLE},
        "records": {
            "accepted_input_rows": accepted_input_rows,
            "canonical_records": len(batch.records),
            "indexed_records": indexed_record_count,
            "unique_record_keys": len(record_keys),
            "duplicate_rows_collapsed": batch.duplicate_rows_collapsed,
            "quarantined_rows": batch.quarantined_input_count,
            "malformed_array_rows": reason_counts["invalid_array"],
            "quarantine_reasons": dict(sorted(reason_counts.items())),
        },
        "claims": {
            "asserted_claims": len(batch.claims),
            "by_capability": {
                capability: claim_counts[capability] for capability in ASSERTION_ALIASES
            },
        },
        "region": {
            "source_field": "address_stateOrRegion",
            "canonical_values": len(OFFICIAL_REGIONS),
            "alias_values": len(REGION_ALIASES),
            "resolved_records": resolved_regions,
            "unresolved_records": indexed_record_count - resolved_regions,
        },
        "validation": validation,
    }


def _apply_live_reconciliation(audit: dict[str, Any]) -> dict[str, Any]:
    source = audit["source"]
    records = audit["records"]
    measurements_match = (
        source["input_rows"] == 10_088
        and records["accepted_input_rows"] == 10_085
        and records["duplicate_rows_collapsed"] == 11
        and records["quarantined_rows"] == 3
        and records["malformed_array_rows"] == 3
    )
    audit["live_reconciliation"] = {
        "documented_quarantine_rows": 3,
        "observed_quarantine_rows": records["quarantined_rows"],
        "matches_measured_source_contract": measurements_match,
    }
    if not measurements_match:
        audit["status"] = "fail"
    return audit


def _warehouse_id(workspace: WorkspaceClient) -> str:
    warehouses = tuple(workspace.warehouses.list())
    if len(warehouses) != 1 or not warehouses[0].id:
        raise RuntimeError("expected exactly one SQL warehouse")
    return warehouses[0].id


def _append_chunk(
    rows: list[dict[str, Any]],
    columns: tuple[str, ...],
    chunk: ResultData,
) -> None:
    for values in chunk.data_array or []:
        rows.append(dict(zip(columns, values, strict=True)))
    for link in chunk.external_links or []:
        if link.external_link is None:
            raise RuntimeError("facility query returned an empty external link")
        headers = {
            name: value
            for name, value in (link.http_headers or {}).items()
            if name.casefold() != "authorization"
        }
        request = Request(link.external_link, headers=headers)
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if not isinstance(payload, list) or any(not isinstance(values, list) for values in payload):
            raise RuntimeError("facility query returned malformed external data")
        for values in payload:
            rows.append(dict(zip(columns, values, strict=True)))


def load_live_rows(workspace: WorkspaceClient, warehouse_id: str) -> tuple[dict[str, Any], ...]:
    """Read only the ingest fields; callers must not log or persist the returned raw rows."""
    response = workspace.statement_execution.execute_statement(
        SOURCE_QUERY,
        warehouse_id,
        disposition=Disposition.EXTERNAL_LINKS,
        format=Format.JSON_ARRAY,
        row_limit=20_000,
        wait_timeout="50s",
    )
    state = getattr(response.status, "state", None)
    if state is None or state.value != "SUCCEEDED":
        raise RuntimeError("facility query did not succeed")
    if response.manifest is None or response.manifest.schema is None:
        raise RuntimeError("facility query returned no schema")
    if response.manifest.truncated:
        raise RuntimeError("facility query result was truncated")
    if response.result is None or response.statement_id is None:
        raise RuntimeError("facility query returned no data")

    schema_columns = response.manifest.schema.columns or []
    columns = tuple(column.name for column in schema_columns if column.name is not None)
    if len(columns) != len(schema_columns):
        raise RuntimeError("facility query returned an unnamed column")

    rows: list[dict[str, Any]] = []
    chunk = response.result
    while True:
        _append_chunk(rows, columns, chunk)
        next_chunk_index = chunk.next_chunk_index
        if next_chunk_index is None and chunk.external_links:
            next_chunk_index = chunk.external_links[-1].next_chunk_index
        if next_chunk_index is None:
            break
        chunk = workspace.statement_execution.get_statement_result_chunk_n(
            response.statement_id,
            next_chunk_index,
        )

    expected_rows = response.manifest.total_row_count
    if expected_rows is not None and len(rows) != expected_rows:
        raise RuntimeError("facility query row count did not match its manifest")
    return tuple(rows)


def write_audit(path: Path, audit: Mapping[str, Any]) -> None:
    """Write a pre-aggregated audit object; raw rows are not accepted at this boundary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")


def run_live_audit(profile: str = DEFAULT_PROFILE, warehouse_id: str | None = None) -> dict[str, Any]:
    """Load the live table, ingest it, and return aggregate evidence only."""
    workspace = WorkspaceClient(profile=profile)
    rows = load_live_rows(workspace, warehouse_id or _warehouse_id(workspace))
    return _apply_live_reconciliation(build_audit(ingest_rows(rows)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a sanitized live ingest audit")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--warehouse-id")
    parser.add_argument("--output", type=Path, default=DEFAULT_AUDIT_PATH)
    args = parser.parse_args()

    try:
        audit = run_live_audit(args.profile, args.warehouse_id)
        write_audit(args.output, audit)
    except Exception as error:
        failure = {
            "schema_version": 1,
            "status": "fail",
            "failure": {"stage": "live_audit", "error_type": type(error).__name__},
        }
        write_audit(args.output, failure)
        print(f"ingest audit: fail ({type(error).__name__})", file=sys.stderr)
        return 1

    if audit.get("status") != "pass":
        print("ingest audit: fail (audit_gate)", file=sys.stderr)
        return 1
    print("ingest audit: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
