"""Behaviour tests for the second-opinion referee."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from trustdesk.ladder import CheckAttempt, CostTier, EvidenceCoordinate, OutcomeKind
from trustdesk.llm_check import ModelReply, ModelRequest, ModelTimeoutError
from trustdesk.marks import Mark
from trustdesk.referee import (
    METHOD_BLANK_RECHECK,
    METHOD_INDEPENDENT_LEXICON,
    METHOD_MODEL_BUNDLE,
    Referee,
    RefereeConfig,
    RefereeConfigurationError,
    RefereeOutcome,
    load_referee_config,
    summarize,
)


def _decision(
    check_id: str = "vocabulary",
    mark: Mark = Mark.SUPPORTS,
    text: str | None = "The hospital runs an intensive care unit.",
    field: str = "description",
    item_index: int = 0,
) -> CheckAttempt:
    return CheckAttempt(
        kind=OutcomeKind.DECISION,
        coordinate=EvidenceCoordinate(field, item_index),
        evidence_text=text,
        mark=mark,
        check_id=check_id,
        implementation_version="1.0.0",
        cost_tier=CostTier.FREE,
        rationale="test decision",
    )


def _abstention() -> CheckAttempt:
    return CheckAttempt(
        kind=OutcomeKind.ABSTENTION,
        coordinate=EvidenceCoordinate("description", 0),
        evidence_text="text",
        mark=None,
        check_id="vocabulary",
        implementation_version="1.0.0",
        cost_tier=CostTier.FREE,
        rationale="cannot tell",
    )


def _rules_referee() -> Referee:
    return Referee(RefereeConfig(enabled=True, mode="rules"))


def _model_reply(items: list[dict[str, object]]) -> str:
    return json.dumps({"items": items})


@dataclass
class ScriptedModelClient:
    """Returns queued replies or exceptions, recording every request."""

    replies: list[str | Exception]

    def __post_init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def classify(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ModelReply(content=reply, endpoint="test", model="test-model", latency_ms=1.0)


# --- disabled and input-contract behaviour ---


def test_disabled_referee_returns_nothing() -> None:
    referee = Referee(RefereeConfig(enabled=False))
    assert referee.referee_claim("ICU", (_decision(),)) == ()


def test_rejects_non_decision_attempts() -> None:
    with pytest.raises(ValueError, match="decisions only"):
        _rules_referee().referee_claim("ICU", (_abstention(),))


def test_empty_decisions_produce_no_findings() -> None:
    assert _rules_referee().referee_claim("ICU", ()) == ()


# --- rules mode ---


def test_blank_decision_agrees_on_recheck() -> None:
    (finding,) = _rules_referee().referee_claim("ICU", (_decision("presence", Mark.MISSING, text=None),))
    assert finding.outcome is RefereeOutcome.AGREE
    assert finding.method == METHOD_BLANK_RECHECK


def test_blank_decision_with_text_disagrees() -> None:
    (finding,) = _rules_referee().referee_claim("ICU", (_decision("presence", Mark.MISSING, text="not blank"),))
    assert finding.outcome is RefereeOutcome.DISAGREE


def test_vocabulary_support_corroborated_only_by_wording_outside_the_deciding_lexicon() -> None:
    # "gynaecology" is an ingest assertion alias but not a vocabulary term, so it
    # survives masking and counts as genuinely independent corroboration.
    decision = _decision(text="Departments: maternity and gynaecology.")
    (finding,) = _rules_referee().referee_claim("maternity", (decision,))
    assert finding.outcome is RefereeOutcome.AGREE
    assert finding.method == METHOD_INDEPENDENT_LEXICON
    assert "gynaecology" in finding.rationale


def test_vocabulary_support_shared_wording_is_not_corroboration() -> None:
    # "intensive care" appears in BOTH term lists; the same word must never both
    # decide and "independently" confirm, so after masking there is nothing left.
    (finding,) = _rules_referee().referee_claim("ICU", (_decision(text="Full intensive care ward."),))
    assert finding.outcome is RefereeOutcome.COULD_NOT_REFEREE
    assert "Only the deciding wording itself matches" in finding.rationale


def test_vocabulary_support_uncorroborated_is_not_error() -> None:
    # "ventilator" is a vocabulary term but not an ingest assertion alias.
    (finding,) = _rules_referee().referee_claim("ICU", (_decision(text="Two ventilator machines."),))
    assert finding.outcome is RefereeOutcome.COULD_NOT_REFEREE
    assert "Not evidence of error" in finding.rationale


def test_unknown_capability_cannot_be_refereed() -> None:
    (finding,) = _rules_referee().referee_claim("dialysis", (_decision(),))
    assert finding.outcome is RefereeOutcome.COULD_NOT_REFEREE


def test_unknown_check_cannot_be_refereed() -> None:
    (finding,) = _rules_referee().referee_claim("ICU", (_decision(check_id="future_check"),))
    assert finding.outcome is RefereeOutcome.COULD_NOT_REFEREE


def test_model_support_decision_with_refuting_language_disagrees() -> None:
    decision = _decision("llama_entailment", Mark.SUPPORTS, "ICU patients are referred to the district hospital.")
    (finding,) = _rules_referee().referee_claim("ICU", (decision,))
    assert finding.outcome is RefereeOutcome.DISAGREE


def test_model_silent_decision_with_literal_mention_disagrees() -> None:
    decision = _decision("llama_entailment", Mark.SILENT, "Has an intensive care unit.")
    (finding,) = _rules_referee().referee_claim("ICU", (decision,))
    assert finding.outcome is RefereeOutcome.DISAGREE


def test_model_conflict_decision_with_refuting_language_agrees() -> None:
    decision = _decision("llama_entailment", Mark.CONFLICTS, "ICU not available at this site.")
    (finding,) = _rules_referee().referee_claim("ICU", (decision,))
    assert finding.outcome is RefereeOutcome.AGREE


def test_findings_preserve_decision_identity_and_order() -> None:
    decisions = (
        _decision("presence", Mark.MISSING, None, "procedure", 0),
        _decision(text="intensive care", field="capability", item_index=1),
    )
    findings = _rules_referee().referee_claim("ICU", decisions)
    assert [finding.coordinate for finding in findings] == [decision.coordinate for decision in decisions]
    assert findings[1].decided_check_id == "vocabulary"
    assert findings[1].decided_mark is Mark.SUPPORTS


# --- model mode ---


def _model_referee(client: ScriptedModelClient, cap: int = 5) -> Referee:
    return Referee(RefereeConfig(enabled=True, mode="model", max_model_bundles=cap), model_client=client)


def test_model_mode_agrees_when_model_supports_the_support_decision() -> None:
    client = ScriptedModelClient(
        [_model_reply([{"field": "description", "item_index": 0, "outcome": "support", "rationale": "clear"}])]
    )
    (finding,) = _model_referee(client).referee_claim("ICU", (_decision(),))
    assert finding.outcome is RefereeOutcome.AGREE
    assert finding.method == METHOD_MODEL_BUNDLE


def test_model_mode_disagreement_and_uncertainty_mapping() -> None:
    client = ScriptedModelClient(
        [
            _model_reply(
                [
                    {"field": "description", "item_index": 0, "outcome": "irrelevant", "rationale": "off-topic"},
                    {"field": "capability", "item_index": 1, "outcome": "uncertain", "rationale": "unclear"},
                ]
            )
        ]
    )
    decisions = (
        _decision(),
        _decision(text="intensive care", field="capability", item_index=1),
    )
    first, second = _model_referee(client).referee_claim("ICU", decisions)
    assert first.outcome is RefereeOutcome.DISAGREE
    assert second.outcome is RefereeOutcome.COULD_NOT_REFEREE


def test_model_mode_never_referees_the_model_itself() -> None:
    client = ScriptedModelClient([])
    decision = _decision("llama_entailment", Mark.SUPPORTS, "Has an intensive care unit.")
    (finding,) = _model_referee(client).referee_claim("ICU", (decision,))
    assert finding.method == METHOD_INDEPENDENT_LEXICON
    assert client.requests == []


def test_model_mode_rechecks_blanks_without_model_calls() -> None:
    client = ScriptedModelClient([])
    (finding,) = _model_referee(client).referee_claim("ICU", (_decision("presence", Mark.MISSING, None),))
    assert finding.method == METHOD_BLANK_RECHECK
    assert client.requests == []


def test_model_cap_falls_back_to_rules_after_budget() -> None:
    client = ScriptedModelClient(
        [_model_reply([{"field": "description", "item_index": 0, "outcome": "support", "rationale": "clear"}])]
    )
    referee = _model_referee(client, cap=1)
    (first,) = referee.referee_claim("ICU", (_decision(),))
    (second,) = referee.referee_claim("ICU", (_decision(text="Full intensive care ward."),))
    assert first.method == METHOD_MODEL_BUNDLE
    assert second.method == METHOD_INDEPENDENT_LEXICON
    assert len(client.requests) == 1


def test_transient_failure_retries_once_then_succeeds() -> None:
    client = ScriptedModelClient(
        [
            ModelTimeoutError(),
            _model_reply([{"field": "description", "item_index": 0, "outcome": "support", "rationale": "clear"}]),
        ]
    )
    (finding,) = _model_referee(client).referee_claim("ICU", (_decision(),))
    assert finding.outcome is RefereeOutcome.AGREE
    assert len(client.requests) == 2


def test_exhausted_transient_retries_become_could_not_referee() -> None:
    client = ScriptedModelClient([ModelTimeoutError(), ModelTimeoutError()])
    (finding,) = _model_referee(client).referee_claim("ICU", (_decision(),))
    assert finding.outcome is RefereeOutcome.COULD_NOT_REFEREE
    assert "failed after one retry" in finding.rationale


def test_malformed_model_output_becomes_could_not_referee() -> None:
    client = ScriptedModelClient(["not json", "still not json"])
    (finding,) = _model_referee(client).referee_claim("ICU", (_decision(),))
    assert finding.outcome is RefereeOutcome.COULD_NOT_REFEREE
    assert "unusable output" in finding.rationale


def test_unexpected_client_failure_never_escapes_the_referee() -> None:
    # Auth errors, missing endpoints, SDK changes: display-only code must not abort a batch.
    client = ScriptedModelClient([RuntimeError("endpoint does not exist")])
    (finding,) = _model_referee(client).referee_claim("ICU", (_decision(),))
    assert finding.outcome is RefereeOutcome.COULD_NOT_REFEREE
    assert "RuntimeError" in finding.rationale


# --- configuration ---


def _config_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "checks.toml"
    path.write_text(content)
    return path


def test_missing_referee_table_means_disabled(tmp_path: Path) -> None:
    path = _config_file(tmp_path, 'checks = ["trustdesk.check_presence:PresenceCheck"]\n')
    assert load_referee_config(path) == RefereeConfig(enabled=False)


def test_config_round_trip(tmp_path: Path) -> None:
    path = _config_file(
        tmp_path,
        '[referee]\nenabled = true\nmode = "model"\nmax_model_bundles = 40\n',
    )
    assert load_referee_config(path) == RefereeConfig(enabled=True, mode="model", max_model_bundles=40)


@pytest.mark.parametrize(
    "table",
    [
        '[referee]\nmode = "vibes"\n',
        '[referee]\nenabled = "yes"\n',
        "[referee]\nmax_model_bundles = -1\n",
        "[referee]\nmax_model_bundles = true\n",
    ],
)
def test_invalid_config_raises(tmp_path: Path, table: str) -> None:
    with pytest.raises(RefereeConfigurationError):
        load_referee_config(_config_file(tmp_path, table))


# --- summary ---


def test_summarize_counts_by_deciding_check() -> None:
    referee = _rules_referee()
    findings = (
        *referee.referee_claim("ICU", (_decision("presence", Mark.MISSING, None),)),
        *referee.referee_claim("maternity", (_decision(text="Maternity and gynaecology wing."),)),
        *referee.referee_claim("ICU", (_decision(text="Two ventilator machines."),)),
    )
    summary = summarize(findings)
    assert summary["decisions_refereed"] == 3
    assert summary["by_deciding_check"]["presence"]["agree"] == 1
    assert summary["by_deciding_check"]["vocabulary"]["agree"] == 1
    assert summary["by_deciding_check"]["vocabulary"]["could_not_referee"] == 1
    assert summary["totals"] == {"agree": 2, "disagree": 0, "could_not_referee": 1}
