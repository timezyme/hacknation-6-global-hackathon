"""Second-opinion referee over check decisions. Never changes a verdict.

Every decision a check produced is re-examined by a method other than the one that
decided it, and the outcome - agree, disagree, or could not referee - is recorded as
receipt data. Disagreement is displayed, never hidden; a decision the referee could
not reach is labelled honestly rather than silently trusted.

Methods, cheapest first:
- blank recheck: a presence decision is re-verified directly against the item text.
- independent lexicon: a vocabulary support decision is corroborated only by wording
  the deciding lexicon could not itself have matched - the deciding lexicon's spans
  are masked before the separately authored assertion aliases in `trustdesk.ingest`
  are consulted, so a shared term can never both decide and "confirm" a decision.
  Model decisions are cross-checked against the vocabulary lexicon, which is
  independent of the model.
- model bundle: when configured, decided items are re-classified by the open-weight
  entailment model, capped per run. The model never referees its own decisions.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from trustdesk import llm_check
from trustdesk.ingest import ASSERTION_PATTERNS
from trustdesk.ladder import (
    DEFAULT_CHECKS_CONFIG,
    CheckAttempt,
    EvidenceCoordinate,
    EvidenceItem,
    OutcomeKind,
)
from trustdesk.lexicon import capability_pattern, find_refutation
from trustdesk.llm_check import ModelClient, ModelRequest, ModelTransientError
from trustdesk.marks import Mark

REFEREE_VERSION = "1.0.0"
METHOD_BLANK_RECHECK = "blank_recheck"
METHOD_INDEPENDENT_LEXICON = "independent_lexicon"
METHOD_MODEL_BUNDLE = "model_bundle"
METHOD_NONE = "none"
_MODES = ("rules", "model")


class RefereeOutcome(StrEnum):
    """The three answers the referee may give about one decision."""

    AGREE = "agree"
    DISAGREE = "disagree"
    COULD_NOT_REFEREE = "could_not_referee"


@dataclass(frozen=True)
class RefereeFinding:
    """Receipt-ready second opinion about one decided evidence coordinate."""

    coordinate: EvidenceCoordinate
    decided_check_id: str
    decided_mark: Mark
    outcome: RefereeOutcome
    method: str
    rationale: str
    referee_version: str = REFEREE_VERSION


@dataclass(frozen=True)
class RefereeConfig:
    """Referee behaviour loaded from the [referee] table of checks.toml."""

    enabled: bool = False
    mode: str = "rules"
    max_model_bundles: int = 0


class RefereeConfigurationError(ValueError):
    """Raised when the [referee] config table cannot be interpreted."""


def load_referee_config(path: Path = DEFAULT_CHECKS_CONFIG) -> RefereeConfig:
    """Read the [referee] table. A missing table means the referee is disabled."""
    try:
        table = tomllib.loads(path.read_text()).get("referee")
    except (OSError, tomllib.TOMLDecodeError):
        raise RefereeConfigurationError("invalid referee configuration") from None
    if table is None:
        return RefereeConfig()
    if not isinstance(table, dict):
        raise RefereeConfigurationError("invalid referee configuration")
    enabled = table.get("enabled", False)
    mode = table.get("mode", "rules")
    max_model_bundles = table.get("max_model_bundles", 0)
    if (
        not isinstance(enabled, bool)
        or mode not in _MODES
        or not isinstance(max_model_bundles, int)
        or isinstance(max_model_bundles, bool)
        or max_model_bundles < 0
    ):
        raise RefereeConfigurationError("invalid referee configuration")
    return RefereeConfig(enabled=enabled, mode=mode, max_model_bundles=max_model_bundles)


def _finding(
    decision: CheckAttempt,
    outcome: RefereeOutcome,
    method: str,
    rationale: str,
) -> RefereeFinding:
    if decision.mark is None:
        raise ValueError("referee accepts decisions only")
    return RefereeFinding(
        coordinate=decision.coordinate,
        decided_check_id=decision.check_id,
        decided_mark=decision.mark,
        outcome=outcome,
        method=method,
        rationale=rationale,
    )


def _not_refereed(decision: CheckAttempt, rationale: str) -> RefereeFinding:
    return _finding(decision, RefereeOutcome.COULD_NOT_REFEREE, METHOD_NONE, rationale)


def _referee_blank(decision: CheckAttempt) -> RefereeFinding:
    text = decision.evidence_text
    if text is None or not text.strip():
        return _finding(
            decision,
            RefereeOutcome.AGREE,
            METHOD_BLANK_RECHECK,
            "Independent recheck confirms the item is blank.",
        )
    return _finding(
        decision,
        RefereeOutcome.DISAGREE,
        METHOD_BLANK_RECHECK,
        "Item was decided blank but contains text on independent recheck.",
    )


def _mask_deciding_lexicon(text: str, capability: str) -> str:
    """Blank out every span the deciding vocabulary lexicon could have matched.

    Corroboration must come from wording the decision could not itself have used.
    Without this, the two term lists' shared terms (16 of 18 assertion aliases) would
    let the same word both decide and "independently" confirm the decision.
    """
    return capability_pattern(capability).sub(lambda match: " " * len(match.group(0)), text)


def _referee_vocabulary_support(decision: CheckAttempt, capability: str) -> RefereeFinding:
    pattern = ASSERTION_PATTERNS.get(capability)
    if pattern is None:
        return _not_refereed(decision, f"No independent term list exists for {capability}.")
    try:
        masked = _mask_deciding_lexicon(decision.evidence_text or "", capability)
    except KeyError:
        return _not_refereed(decision, f"No independent term list exists for {capability}.")
    corroboration = pattern.search(masked)
    if corroboration is not None:
        return _finding(
            decision,
            RefereeOutcome.AGREE,
            METHOD_INDEPENDENT_LEXICON,
            f'A separately authored term list finds independent {capability} wording '
            f'("{corroboration.group(0)}") the deciding lexicon could not have matched.',
        )
    return _finding(
        decision,
        RefereeOutcome.COULD_NOT_REFEREE,
        METHOD_INDEPENDENT_LEXICON,
        "Only the deciding wording itself matches; no independent corroboration. Not evidence of error.",
    )


def _referee_model_decision(decision: CheckAttempt, capability: str) -> RefereeFinding:
    """Cross-check a model decision with the free lexicon, which the model never saw."""
    text = decision.evidence_text or ""
    refutation = find_refutation(text)
    try:
        mention = capability_pattern(capability).search(text)
    except KeyError:
        return _not_refereed(decision, f"No independent term list exists for {capability}.")
    if decision.mark is Mark.SUPPORTS:
        if refutation is not None:
            return _finding(
                decision,
                RefereeOutcome.DISAGREE,
                METHOD_INDEPENDENT_LEXICON,
                f'Model decided supports, but the item contains refuting language "{refutation}".',
            )
        if mention is not None:
            return _finding(
                decision,
                RefereeOutcome.AGREE,
                METHOD_INDEPENDENT_LEXICON,
                f'The free lexicon also finds {capability} support at "{mention.group(0)}".',
            )
        return _finding(
            decision,
            RefereeOutcome.COULD_NOT_REFEREE,
            METHOD_INDEPENDENT_LEXICON,
            "The free lexicon finds no signal either way; that is not evidence of error.",
        )
    if decision.mark is Mark.CONFLICTS:
        if refutation is not None:
            return _finding(
                decision,
                RefereeOutcome.AGREE,
                METHOD_INDEPENDENT_LEXICON,
                f'The free lexicon also finds refuting language "{refutation}".',
            )
        return _finding(
            decision,
            RefereeOutcome.COULD_NOT_REFEREE,
            METHOD_INDEPENDENT_LEXICON,
            "The free lexicon finds no refuting language; that is not evidence of error.",
        )
    if decision.mark is Mark.SILENT:
        if mention is not None:
            return _finding(
                decision,
                RefereeOutcome.DISAGREE,
                METHOD_INDEPENDENT_LEXICON,
                f'Model decided the item says nothing, but it literally mentions "{mention.group(0)}".',
            )
        return _finding(
            decision,
            RefereeOutcome.AGREE,
            METHOD_INDEPENDENT_LEXICON,
            "The free lexicon also finds no capability mention.",
        )
    return _not_refereed(decision, "No independent rule exists for this decision.")


def _referee_with_rules(decision: CheckAttempt, capability: str) -> RefereeFinding:
    if decision.mark is Mark.MISSING:
        return _referee_blank(decision)
    if decision.check_id == "vocabulary" and decision.mark is Mark.SUPPORTS:
        return _referee_vocabulary_support(decision, capability)
    if decision.check_id == "llama_entailment":
        return _referee_model_decision(decision, capability)
    return _not_refereed(decision, "No independent rule exists for this decision.")


_MODEL_AGREEMENT: dict[Mark, dict[str, RefereeOutcome]] = {
    Mark.SUPPORTS: {
        "support": RefereeOutcome.AGREE,
        "conflict": RefereeOutcome.DISAGREE,
        "irrelevant": RefereeOutcome.DISAGREE,
        "uncertain": RefereeOutcome.COULD_NOT_REFEREE,
    },
    Mark.CONFLICTS: {
        "support": RefereeOutcome.DISAGREE,
        "conflict": RefereeOutcome.AGREE,
        "irrelevant": RefereeOutcome.DISAGREE,
        "uncertain": RefereeOutcome.COULD_NOT_REFEREE,
    },
    Mark.SILENT: {
        "support": RefereeOutcome.DISAGREE,
        "conflict": RefereeOutcome.DISAGREE,
        "irrelevant": RefereeOutcome.AGREE,
        "uncertain": RefereeOutcome.COULD_NOT_REFEREE,
    },
}


class Referee:
    """Applies the configured referee method to the decisions of one claim at a time."""

    def __init__(self, config: RefereeConfig, model_client: ModelClient | None = None) -> None:
        self.config = config
        self._model_client = model_client
        self._bundles_used = 0

    def referee_claim(
        self,
        capability: str,
        decisions: Sequence[CheckAttempt],
    ) -> tuple[RefereeFinding, ...]:
        """Return one finding per decision, in input order. Empty when disabled."""
        decisions = tuple(decisions)
        if any(
            decision.kind is not OutcomeKind.DECISION or decision.mark is None for decision in decisions
        ):
            raise ValueError("referee accepts decisions only")
        if not self.config.enabled or not decisions:
            return ()
        if self.config.mode == "model":
            return self._referee_with_model(capability, decisions)
        return tuple(_referee_with_rules(decision, capability) for decision in decisions)

    def _referee_with_model(
        self,
        capability: str,
        decisions: tuple[CheckAttempt, ...],
    ) -> tuple[RefereeFinding, ...]:
        # The model never referees itself, and blanks carry no text worth a model call.
        eligible = tuple(
            decision
            for decision in decisions
            if decision.check_id != "llama_entailment" and decision.mark is not Mark.MISSING
        )
        model_findings: dict[EvidenceCoordinate, RefereeFinding] = {}
        if eligible and self._model_client is not None and self._bundles_used < self.config.max_model_bundles:
            self._bundles_used += 1
            model_findings = self._classify_bundle(capability, eligible)
        return tuple(
            model_findings.get(decision.coordinate, _referee_with_rules(decision, capability))
            for decision in decisions
        )

    def _classify_bundle(
        self,
        capability: str,
        decisions: tuple[CheckAttempt, ...],
    ) -> dict[EvidenceCoordinate, RefereeFinding]:
        items = tuple(EvidenceItem(decision.coordinate, decision.evidence_text) for decision in decisions)
        request = _referee_request(capability, items)
        client = self._model_client
        assert client is not None  # guarded by caller
        for attempt in range(2):
            try:
                reply = client.classify(request)
                parsed = llm_check._parse(reply.content, items)
            except ModelTransientError:
                if attempt == 0:
                    continue
                return {
                    decision.coordinate: _finding(
                        decision,
                        RefereeOutcome.COULD_NOT_REFEREE,
                        METHOD_MODEL_BUNDLE,
                        "Referee model call failed after one retry.",
                    )
                    for decision in decisions
                }
            except (TypeError, ValueError):
                if attempt == 0:
                    continue
                return {
                    decision.coordinate: _finding(
                        decision,
                        RefereeOutcome.COULD_NOT_REFEREE,
                        METHOD_MODEL_BUNDLE,
                        "Referee model returned unusable output after one retry.",
                    )
                    for decision in decisions
                }
            except Exception as error:
                # Display-only component: an unexpected client failure (auth, missing
                # endpoint, SDK change) must never abort the batch that carries it.
                return {
                    decision.coordinate: _finding(
                        decision,
                        RefereeOutcome.COULD_NOT_REFEREE,
                        METHOD_MODEL_BUNDLE,
                        f"Referee model client failed ({type(error).__name__}).",
                    )
                    for decision in decisions
                }
            outcomes = {item.coordinate: item for item in parsed}
            return {
                decision.coordinate: self._model_finding(decision, outcomes[decision.coordinate])
                for decision in decisions
            }
        raise AssertionError("unreachable")

    @staticmethod
    def _model_finding(decision: CheckAttempt, parsed: llm_check._ModelFinding) -> RefereeFinding:
        if decision.mark is None:
            raise ValueError("referee accepts decisions only")
        outcome = _MODEL_AGREEMENT[decision.mark][parsed.outcome]
        return _finding(
            decision,
            outcome,
            METHOD_MODEL_BUNDLE,
            f"Model referee read this item as {parsed.outcome}: {parsed.rationale}",
        )


def _referee_request(capability: str, items: tuple[EvidenceItem, ...]) -> ModelRequest:
    payload = {
        "capability": capability,
        "purpose": "referee-v1",
        "items": [
            {"field": item.coordinate.field, "item_index": item.coordinate.item_index, "text": item.text}
            for item in items
        ],
    }
    request_id = sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return ModelRequest(request_id=request_id, capability=capability, items=items)


def summarize(findings: Sequence[RefereeFinding]) -> dict[str, Any]:
    """Aggregate referee outcomes per deciding check, for the artifact and method panel."""
    by_check: dict[str, dict[str, int]] = {}
    for finding in findings:
        counts = by_check.setdefault(
            finding.decided_check_id,
            {outcome.value: 0 for outcome in RefereeOutcome},
        )
        counts[finding.outcome.value] += 1
    return {
        "referee_version": REFEREE_VERSION,
        "decisions_refereed": len(findings),
        "by_deciding_check": by_check,
        "totals": {
            outcome.value: sum(1 for finding in findings if finding.outcome is outcome)
            for outcome in RefereeOutcome
        },
    }


def _decision_from_receipt_item(item: dict[str, Any]) -> CheckAttempt:
    from trustdesk.ladder import CostTier

    return CheckAttempt(
        kind=OutcomeKind.DECISION,
        coordinate=EvidenceCoordinate(str(item["field"]), int(item["item_index"])),
        evidence_text=item.get("text"),
        mark=Mark(str(item["mark"])),
        check_id=str(item["deciding_check"]),
        implementation_version=str(item.get("check_version", "0.0.0")),
        cost_tier=CostTier.FREE,
        rationale=str(item.get("rationale", "reconstructed from receipt")),
    )


def _run_over_published_receipts(profile: str, artifact_path: Path) -> dict[str, Any]:
    """Referee the active published run's decisions and write an aggregate artifact."""
    from datetime import UTC, datetime

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import Disposition, Format, StatementState

    workspace = WorkspaceClient(profile=profile)
    warehouses = tuple(workspace.warehouses.list())
    if len(warehouses) != 1 or not warehouses[0].id:
        raise RuntimeError("expected exactly one SQL warehouse")
    response = workspace.statement_execution.execute_statement(
        """SELECT capability, receipt_json FROM workspace.default.trustdesk_walking_skeleton
           WHERE run_status = 'complete'
             AND published_at = (SELECT MAX(published_at) FROM workspace.default.trustdesk_walking_skeleton
                                 WHERE run_status = 'complete')""",
        warehouses[0].id,
        format=Format.JSON_ARRAY,
        disposition=Disposition.INLINE,
        wait_timeout="50s",
    )
    state = response.status.state if response.status else None
    if state is not StatementState.SUCCEEDED or response.result is None:
        raise RuntimeError(f"receipt query failed with state {state}")

    referee = Referee(RefereeConfig(enabled=True, mode="rules"))
    findings: list[RefereeFinding] = []
    for capability, receipt_json in response.result.data_array or []:
        decisions = tuple(
            _decision_from_receipt_item(item)
            for item in json.loads(receipt_json)
            if item.get("outcome") == "decision"
        )
        findings.extend(referee.referee_claim(capability, decisions))

    artifact = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "active published walking-skeleton run",
        "mode": "rules",
        **summarize(tuple(findings)),
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Referee the active published run's decisions.")
    parser.add_argument("--profile", default="trustdesk-spike")
    parser.add_argument("--artifact", type=Path, default=Path("artifacts/referee-summary.json"))
    arguments = parser.parse_args()
    result = _run_over_published_receipts(arguments.profile, arguments.artifact)
    print(json.dumps(result["totals"], sort_keys=True))
