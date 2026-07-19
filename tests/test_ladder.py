"""Contract tests for configured, independently replaceable evidence checks."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from trustdesk.check_presence import PresenceCheck
from trustdesk.check_vocabulary import VocabularyCheck
from trustdesk.ladder import (
    CheckConfigurationError,
    CheckFinding,
    ClaimEvidence,
    CostTier,
    OutcomeKind,
    load_checks,
    run_checks,
)
from trustdesk.marks import Mark
from trustdesk.models import Claim, FacilityRecord


def claim_evidence(
    *,
    description: str | None = "A hospital record.",
    capability: tuple[str, ...] = ("General medicine",),
    equipment: tuple[str, ...] = ("X-ray machine",),
    procedure: tuple[str, ...] = ("Routine pathology",),
    target: str = "ICU",
) -> ClaimEvidence:
    record = FacilityRecord(
        record_key="record-1",
        facility_id="facility-1",
        name="Example Hospital",
        description=description,
        capability=capability,
        procedure=procedure,
        equipment=equipment,
        source_urls=(),
        region="Bihar",
    )
    return ClaimEvidence.from_record(Claim(record.record_key, target), record)


def test_complete_bundle_is_decided_item_by_item_with_stable_receipt_metadata():
    evidence = claim_evidence(
        description=None,
        capability=("12-bed intensive care unit",),
        equipment=("X-ray machine",),
        procedure=(),
    )

    result = run_checks(evidence, (PresenceCheck(), VocabularyCheck()))

    decisions = {(attempt.coordinate.field, attempt.coordinate.item_index): attempt for attempt in result.decisions}
    assert decisions[("description", 0)].mark is Mark.MISSING
    assert decisions[("description", 0)].check_id == "presence"
    assert decisions[("capability", 0)].mark is Mark.SUPPORTS
    assert decisions[("capability", 0)].check_id == "vocabulary"
    assert decisions[("procedure", 0)].mark is Mark.MISSING
    assert {(item.coordinate.field, item.coordinate.item_index) for item in result.unresolved} == {("equipment", 0)}

    capability_attempts = [
        attempt for attempt in result.attempt_history if attempt.coordinate.field == "capability"
    ]
    assert [attempt.kind for attempt in capability_attempts] == [OutcomeKind.ABSTENTION, OutcomeKind.DECISION]
    assert all(attempt.implementation_version == "1.0.0" for attempt in capability_attempts)
    assert all(attempt.cost_tier is CostTier.FREE for attempt in capability_attempts)
    assert all(attempt.rationale.strip() for attempt in result.attempt_history)
    assert capability_attempts[-1].evidence_text == "12-bed intensive care unit"


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t "])
def test_absent_text_is_missing_not_silent(empty: str | None):
    result = run_checks(claim_evidence(description=empty), (PresenceCheck(),))

    description = next(attempt for attempt in result.decisions if attempt.coordinate.field == "description")
    assert description.mark is Mark.MISSING
    assert description.span is None


def test_vocabulary_non_match_and_refutation_abstain_instead_of_guessing():
    evidence = claim_evidence(
        description="All ICU cases are referred to Patna; no intensive care unit is maintained on site.",
    )

    result = run_checks(evidence, (PresenceCheck(), VocabularyCheck()))

    description_attempts = [
        attempt for attempt in result.attempt_history if attempt.coordinate.field == "description"
    ]
    assert [attempt.kind for attempt in description_attempts] == [OutcomeKind.ABSTENTION, OutcomeKind.ABSTENTION]
    assert {item.coordinate.field for item in result.unresolved} == {
        "description",
        "capability",
        "equipment",
        "procedure",
    }


def test_abbreviation_long_form_and_word_boundaries_are_preserved():
    evidence = claim_evidence(
        description="ICU and intensive care available.",
        capability=("Auricular surgery and epicural care",),
    )

    result = run_checks(evidence, (PresenceCheck(), VocabularyCheck()))

    decisions = {(attempt.coordinate.field, attempt.coordinate.item_index): attempt for attempt in result.decisions}
    description = decisions[("description", 0)]
    assert description.mark is Mark.SUPPORTS
    assert description.span is not None
    assert description.evidence_text is not None
    assert description.evidence_text[slice(*description.span)].lower() == "icu"
    assert ("capability", 0) not in decisions


@pytest.mark.parametrize(
    ("target", "text"),
    [
        (
            "maternity",
            "The maternity wing has been closed since renovation began and patients are directed elsewhere.",
        ),
        (
            "emergency",
            "The clinic has no emergency or after-hours cover; emergencies are diverted to the district hospital.",
        ),
    ],
)
def test_closure_and_negation_language_abstain(target: str, text: str):
    result = run_checks(claim_evidence(description=text, target=target), (PresenceCheck(), VocabularyCheck()))

    description_attempts = [
        attempt for attempt in result.attempt_history if attempt.coordinate.field == "description"
    ]
    assert [attempt.kind for attempt in description_attempts] == [OutcomeKind.ABSTENTION, OutcomeKind.ABSTENTION]


def test_receiving_referrals_is_not_a_refutation():
    text = "Referral hospital serving the Kosi belt. Intensive care and post-operative monitoring available."
    result = run_checks(claim_evidence(description=text), (PresenceCheck(), VocabularyCheck()))

    description = next(attempt for attempt in result.decisions if attempt.coordinate.field == "description")
    assert description.mark is Mark.SUPPORTS
    assert description.check_id == "vocabulary"


class FirstCheck:
    check_id = "first"
    implementation_version = "1.2.3"
    cost_tier = CostTier.FREE

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        self.calls += 1
        return tuple(
            CheckFinding(
                kind=OutcomeKind.DECISION if item.coordinate.field == "description" else OutcomeKind.ABSTENTION,
                coordinate=item.coordinate,
                mark=Mark.SUPPORTS if item.coordinate.field == "description" else None,
                rationale="first check result",
            )
            for item in evidence.items
        )


class SecondCheck:
    check_id = "second"
    implementation_version = "2.0.0"
    cost_tier = CostTier.METERED

    def __init__(self) -> None:
        self.calls = 0
        self.seen_fields: tuple[str, ...] = ()

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        self.calls += 1
        self.seen_fields = tuple(item.coordinate.field for item in evidence.items)
        return tuple(
            CheckFinding(
                kind=OutcomeKind.DECISION,
                coordinate=item.coordinate,
                mark=Mark.SILENT,
                rationale="second check result",
            )
            for item in evidence.items
        )


def test_each_check_runs_once_and_first_decision_wins_per_coordinate():
    first = FirstCheck()
    second = SecondCheck()

    result = run_checks(claim_evidence(), (second, first))

    assert first.calls == 1
    assert second.calls == 1
    assert "description" not in second.seen_fields
    description = next(attempt for attempt in result.decisions if attempt.coordinate.field == "description")
    assert description.check_id == "first"
    assert len(result.decisions) == 4
    assert result.unresolved == ()


class BrokenCheck:
    check_id = "broken"
    implementation_version = "1.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        raise RuntimeError("secret-bearing exception detail")


def test_processing_failure_is_recorded_without_leaking_exception_text_and_does_not_block_fallback():
    result = run_checks(claim_evidence(), (BrokenCheck(), SecondCheck()))

    failures = [attempt for attempt in result.attempt_history if attempt.check_id == "broken"]
    assert len(failures) == 4
    assert all(attempt.kind is OutcomeKind.PROCESSING_FAILURE for attempt in failures)
    assert all("RuntimeError" in attempt.rationale for attempt in failures)
    assert all("secret-bearing" not in attempt.rationale for attempt in failures)
    assert len(result.decisions) == 4
    assert result.unresolved == ()


class SelectiveCheck:
    check_id = "selective"
    implementation_version = "1.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        item = evidence.items[0]
        return (
            CheckFinding(
                kind=OutcomeKind.DECISION,
                coordinate=item.coordinate,
                mark=Mark.SUPPORTS,
                rationale="selective decision",
            ),
        )


def test_omitted_findings_are_normalized_to_abstentions():
    result = run_checks(claim_evidence(), (SelectiveCheck(),))

    assert len(result.attempt_history) == 4
    assert result.attempt_history[0].kind is OutcomeKind.DECISION
    assert all(attempt.kind is OutcomeKind.ABSTENTION for attempt in result.attempt_history[1:])
    assert len(result.unresolved) == 3


class InvalidKindCheck:
    check_id = "invalid_kind"
    implementation_version = "1.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        return tuple(
            CheckFinding(
                kind=cast(OutcomeKind, "invented_outcome"),
                coordinate=item.coordinate,
                mark=None,
                rationale="invalid outcome kind",
            )
            for item in evidence.items
        )


class InvalidMarkCheck:
    check_id = "invalid_mark"
    implementation_version = "1.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        return tuple(
            CheckFinding(
                kind=OutcomeKind.DECISION,
                coordinate=item.coordinate,
                mark=cast(Mark, "invented_mark"),
                rationale="invalid mark",
            )
            for item in evidence.items
        )


@pytest.mark.parametrize("check", [InvalidKindCheck(), InvalidMarkCheck()])
def test_runtime_invalid_outcome_values_become_processing_failures(check: InvalidKindCheck | InvalidMarkCheck):
    result = run_checks(claim_evidence(), (check,))

    assert len(result.attempt_history) == 4
    assert all(attempt.kind is OutcomeKind.PROCESSING_FAILURE for attempt in result.attempt_history)
    assert len(result.unresolved) == 4


def test_unknown_capability_becomes_visible_processing_failure_not_a_crash():
    result = run_checks(claim_evidence(target="intensive-care"), (PresenceCheck(), VocabularyCheck()))

    failures = [attempt for attempt in result.attempt_history if attempt.check_id == "vocabulary"]
    assert len(failures) == 4
    assert all(attempt.kind is OutcomeKind.PROCESSING_FAILURE for attempt in failures)
    assert len(result.unresolved) == 4


def test_default_config_loads_named_checks_in_declared_order():
    checks = load_checks(Path("config/checks.toml"))

    assert [check.check_id for check in checks] == ["presence", "vocabulary"]


def test_unknown_check_configuration_is_rejected_generically(tmp_path: Path):
    config = tmp_path / "checks.toml"
    config.write_text('checks = ["not_a_real_module:MissingCheck"]\n')

    with pytest.raises(CheckConfigurationError, match="invalid check configuration") as error:
        load_checks(config)

    assert "not_a_real_module" not in str(error.value)


def test_new_check_is_one_implementation_file_and_one_config_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = tmp_path / "test_only_check.py"
    module.write_text(
        """\
from trustdesk.ladder import CheckFinding, ClaimEvidence, CostTier, OutcomeKind
from trustdesk.marks import Mark

class TestOnlyCheck:
    check_id = "test_only"
    implementation_version = "7.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        return tuple(
            CheckFinding(
                kind=OutcomeKind.DECISION,
                coordinate=item.coordinate,
                mark=Mark.CONFLICTS,
                rationale="test-only configured check",
            )
            for item in evidence.items
        )
"""
    )
    config = tmp_path / "checks.toml"
    config.write_text('checks = ["test_only_check:TestOnlyCheck"]\n')
    monkeypatch.syspath_prepend(str(tmp_path))

    checks = load_checks(config)
    result = run_checks(claim_evidence(), checks)

    assert [check.check_id for check in checks] == ["test_only"]
    assert {attempt.check_id for attempt in result.decisions} == {"test_only"}
    assert {attempt.mark for attempt in result.decisions} == {Mark.CONFLICTS}
    assert result.unresolved == ()

    config.write_text('checks = ["trustdesk.check_presence:PresenceCheck"]\n')
    checks_without_extension = load_checks(config)
    assert [check.check_id for check in checks_without_extension] == ["presence"]


def test_configured_checks_are_ordered_by_cost_then_config_position(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = tmp_path / "ordered_checks.py"
    module.write_text(
        """\
from trustdesk.ladder import CostTier

class MeteredCheck:
    check_id = "metered"
    implementation_version = "1.0.0"
    cost_tier = CostTier.METERED

    def evaluate(self, evidence):
        return ()

class FreeCheck:
    check_id = "free"
    implementation_version = "1.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence):
        return ()
"""
    )
    config = tmp_path / "checks.toml"
    config.write_text(
        'checks = ["ordered_checks:MeteredCheck", "ordered_checks:FreeCheck"]\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    checks = load_checks(config)

    assert [check.check_id for check in checks] == ["free", "metered"]
