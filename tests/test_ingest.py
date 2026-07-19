"""Safe ingestion of synthetic facility rows through the public batch boundary."""

import json
import sys
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import trustdesk.ingest as ingest_module
from trustdesk.ingest import build_audit, ingest_rows, load_live_rows, main

FACILITY_ID = "00000000-0000-4000-8000-000000000001"


def facility_row(**overrides):
    row = {
        "unique_id": FACILITY_ID,
        "name": "  District Hospital  ",
        "description": "Open 24 hours.",
        "capability": '["Intensive Care Unit", "General Medicine"]',
        "procedure": '["Emergency surgery", ""]',
        "equipment": '["Ventilator"]',
        "source_urls": '["https://example.test/facility"]',
        "address_stateOrRegion": "  Bihar  ",
        "address_country": "India",
        "address_countryCode": "IN",
    }
    return {**row, **overrides}


def test_valid_row_becomes_value_object_and_asserted_claim_only():
    batch = ingest_rows([facility_row()])

    assert batch.input_count == 1
    assert batch.quarantined == ()
    assert batch.duplicate_rows_collapsed == 0
    assert len(batch.records) == 1

    record = batch.records[0]
    assert record.record_key.startswith("record:")
    assert record.facility_id == FACILITY_ID
    assert record.name == "District Hospital"
    assert record.region == "Bihar"
    assert record.capability == ("Intensive Care Unit", "General Medicine")
    assert record.procedure == ("Emergency surgery",)
    assert record.equipment == ("Ventilator",)
    assert record.source_urls == ("https://example.test/facility",)
    assert [(claim.record_key, claim.capability) for claim in batch.claims] == [
        (record.record_key, "ICU")
    ]


def test_structurally_invalid_row_is_quarantined_without_leaking_raw_values():
    batch = ingest_rows(
        [
            facility_row(
                unique_id="",
                address_country=None,
                address_countryCode=None,
                capability="{broken json",
            )
        ]
    )

    assert batch.input_count == 1
    assert batch.records == ()
    assert batch.claims == ()
    assert len(batch.quarantined) == 1
    quarantined = batch.quarantined[0]
    assert quarantined.record_key.startswith("quarantine:")
    assert quarantined.record_key == ingest_rows(
        [
            facility_row(
                unique_id="",
                address_country=None,
                address_countryCode=None,
                capability="{broken json",
            )
        ]
    ).quarantined[0].record_key
    assert quarantined.facility_id is None
    assert quarantined.region == "Bihar"
    assert quarantined.asserted_capabilities == ()
    assert quarantined.reasons == (
        "invalid_array",
        "invalid_identifier",
    )


def test_quarantine_preserves_recoverable_identity_region_and_asserted_claim():
    batch = ingest_rows([facility_row(procedure="{broken json")])

    quarantined = batch.quarantined[0]
    assert quarantined.record_key.startswith("record:")
    assert quarantined.facility_id == FACILITY_ID
    assert quarantined.region == "Bihar"
    assert quarantined.asserted_capabilities == ("ICU",)
    assert quarantined.reasons == ("invalid_array",)
    assert [(claim.record_key, claim.capability) for claim in batch.claims] == [
        (quarantined.record_key, "ICU")
    ]


def test_duplicate_ids_collapse_identical_rows_and_distinguish_different_rows_stably():
    first = facility_row()
    different = facility_row(name="Regional Medical Centre")

    forward = ingest_rows([first, dict(first), different])
    reversed_batch = ingest_rows([different, dict(first), first])

    assert forward.input_count == 3
    assert forward.duplicate_rows_collapsed == 1
    assert len(forward.records) == 2
    assert len({record.record_key for record in forward.records}) == 2
    assert all(record.record_key.startswith("record:") for record in forward.records)
    assert [record.record_key for record in forward.records] == [
        record.record_key for record in reversed_batch.records
    ]
    assert forward.claims == reversed_batch.claims


def test_byte_identical_quarantined_rows_collapse_before_claim_generation():
    invalid = facility_row(procedure="{broken json")

    batch = ingest_rows([invalid, dict(invalid)])

    assert batch.input_count == 2
    assert batch.accepted_input_count == 0
    assert batch.quarantined_input_count == 2
    assert batch.duplicate_rows_collapsed == 1
    assert len(batch.quarantined) == 1
    assert len(batch.claims) == 1
    assert build_audit(batch)["status"] == "pass"


def test_null_and_blank_array_items_are_removed_without_quarantining_the_row():
    batch = ingest_rows(
        [
            facility_row(
                capability='["ICU", null, "", "  "]',
                procedure="null",
                equipment="[]",
            )
        ]
    )

    assert batch.quarantined == ()
    assert batch.records[0].capability == ("ICU",)
    assert batch.records[0].procedure == ()
    assert batch.records[0].equipment == ()


def test_claim_source_recognizes_six_direct_assertions_but_not_indirect_evidence_terms():
    other_id = "00000000-0000-4000-8000-000000000002"
    batch = ingest_rows(
        [
            facility_row(
                capability='["Intensive Care Unit", "Maternity", "Emergency", "Oncology", "Trauma", "NICU"]'
            ),
            facility_row(
                unique_id=other_id,
                capability='["Ventilator", "Palliative care", "Fracture service", "Newborn care"]',
            ),
        ]
    )

    direct_key = next(record.record_key for record in batch.records if record.facility_id == FACILITY_ID)
    assert [(claim.record_key, claim.capability) for claim in batch.claims] == [
        (direct_key, "ICU"),
        (direct_key, "maternity"),
        (direct_key, "emergency"),
        (direct_key, "oncology"),
        (direct_key, "trauma"),
        (direct_key, "NICU"),
    ]


def test_nicu_assertion_does_not_invent_a_separate_icu_claim():
    batch = ingest_rows([facility_row(capability='["Neonatal Intensive Care Unit"]')])

    assert [(claim.record_key, claim.capability) for claim in batch.claims] == [
        (batch.records[0].record_key, "NICU")
    ]


def test_region_uses_official_state_names_and_keeps_dirty_labels_unresolved():
    other_id = "00000000-0000-4000-8000-000000000002"
    batch = ingest_rows(
        [
            facility_row(address_stateOrRegion="Orissa"),
            facility_row(unique_id=other_id, address_stateOrRegion="Near northern border"),
        ]
    )

    assert batch.quarantined == ()
    assert [record.region for record in batch.records] == ["Odisha", None]


def test_audit_contains_aggregate_proof_and_no_raw_row_values():
    batch = ingest_rows(
        [
            facility_row(),
            facility_row(
                unique_id="",
                name="Private Facility Name",
                address_country=None,
                address_countryCode=None,
                capability="{broken json",
                source_urls='["https://private.example/source"]',
            ),
        ]
    )

    audit = build_audit(batch)

    assert audit == {
        "schema_version": 1,
        "status": "pass",
        "source": {
            "input_rows": 2,
            "table": "virtue_foundation_dais_2026.virtue_foundation_dataset.facilities",
        },
        "records": {
            "accepted_input_rows": 1,
            "canonical_records": 1,
            "indexed_records": 2,
            "unique_record_keys": 2,
            "duplicate_rows_collapsed": 0,
            "quarantined_rows": 1,
            "malformed_array_rows": 1,
            "quarantine_reasons": {
                "invalid_array": 1,
                "invalid_identifier": 1,
            },
        },
        "claims": {
            "asserted_claims": 1,
            "by_capability": {
                "ICU": 1,
                "maternity": 0,
                "emergency": 0,
                "oncology": 0,
                "trauma": 0,
                "NICU": 0,
            },
        },
        "region": {
            "source_field": "address_stateOrRegion",
            "canonical_values": 36,
            "alias_values": 5,
            "resolved_records": 2,
            "unresolved_records": 0,
        },
        "validation": {
            "claim_keys_known": True,
            "claims_unique": True,
            "duplicate_count_reconciled": True,
            "input_count_reconciled": True,
            "record_keys_unique": True,
        },
    }
    serialized = json.dumps(audit)
    assert "Private Facility Name" not in serialized
    assert "https://private.example/source" not in serialized


def test_audit_fails_when_batch_counts_do_not_reconcile():
    batch = ingest_rows([facility_row()])

    audit = build_audit(replace(batch, input_count=2))

    assert audit["status"] == "fail"
    assert audit["validation"] == {
        "claim_keys_known": True,
        "claims_unique": True,
        "duplicate_count_reconciled": False,
        "input_count_reconciled": False,
        "record_keys_unique": True,
    }


def test_live_loader_follows_external_result_chunks_without_authorization_header(monkeypatch):
    first_chunk = SimpleNamespace(
        data_array=None,
        external_links=[
            SimpleNamespace(
                external_link="https://chunks.example/first",
                http_headers={"Authorization": "must-not-forward", "x-test": "first"},
                next_chunk_index=1,
            )
        ],
        next_chunk_index=None,
    )
    second_chunk = SimpleNamespace(
        data_array=None,
        external_links=[
            SimpleNamespace(
                external_link="https://chunks.example/second",
                http_headers={"x-test": "second"},
                next_chunk_index=None,
            )
        ],
        next_chunk_index=None,
    )
    response = SimpleNamespace(
        status=SimpleNamespace(state=SimpleNamespace(value="SUCCEEDED")),
        manifest=SimpleNamespace(
            schema=SimpleNamespace(
                columns=[SimpleNamespace(name="unique_id"), SimpleNamespace(name="name")]
            ),
            truncated=False,
            total_row_count=2,
        ),
        result=first_chunk,
        statement_id="statement-1",
    )

    class StatementExecution:
        def execute_statement(self, *args, **kwargs):
            return response

        def get_statement_result_chunk_n(self, statement_id, chunk_index):
            assert (statement_id, chunk_index) == ("statement-1", 1)
            return second_chunk

    payloads = {
        "https://chunks.example/first": b'[["id-1", "First"]]',
        "https://chunks.example/second": b'[["id-2", "Second"]]',
    }

    def open_external(request, timeout):
        assert timeout == 30
        assert request.get_header("Authorization") is None
        return BytesIO(payloads[request.full_url])

    monkeypatch.setattr(ingest_module, "urlopen", open_external)
    workspace = SimpleNamespace(statement_execution=StatementExecution())

    assert load_live_rows(workspace, "warehouse-1") == (
        {"unique_id": "id-1", "name": "First"},
        {"unique_id": "id-2", "name": "Second"},
    )


def test_cli_writes_pass_artifact_without_raw_rows(monkeypatch, tmp_path, capsys):
    output = tmp_path / "audit.json"
    monkeypatch.setattr(ingest_module, "run_live_audit", lambda profile, warehouse_id: {"status": "pass"})
    monkeypatch.setattr(sys, "argv", ["trustdesk-ingest", "--output", str(output)])

    assert main() == 0
    assert json.loads(output.read_text()) == {"status": "pass"}
    assert capsys.readouterr().out == "ingest audit: pass\n"


def test_cli_returns_failure_when_audit_gate_is_red(monkeypatch, tmp_path, capsys):
    output = tmp_path / "audit.json"
    monkeypatch.setattr(ingest_module, "run_live_audit", lambda profile, warehouse_id: {"status": "fail"})
    monkeypatch.setattr(sys, "argv", ["trustdesk-ingest", "--output", str(output)])

    assert main() == 1
    assert json.loads(output.read_text()) == {"status": "fail"}
    assert capsys.readouterr().err == "ingest audit: fail (audit_gate)\n"


def test_cli_failure_artifact_and_stderr_redact_exception_message(monkeypatch, tmp_path, capsys):
    output = tmp_path / "audit.json"

    def fail_audit(profile, warehouse_id):
        raise RuntimeError("secret host and raw facility value")

    monkeypatch.setattr(ingest_module, "run_live_audit", fail_audit)
    monkeypatch.setattr(sys, "argv", ["trustdesk-ingest", "--output", str(output)])

    assert main() == 1
    assert json.loads(output.read_text()) == {
        "schema_version": 1,
        "status": "fail",
        "failure": {"stage": "live_audit", "error_type": "RuntimeError"},
    }
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ingest audit: fail (RuntimeError)\n"
