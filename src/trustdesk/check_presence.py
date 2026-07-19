"""Free check that decides only whether an evidence item is missing."""

from __future__ import annotations

from trustdesk.ladder import CheckFinding, ClaimEvidence, CostTier, EvidenceCoordinate, OutcomeKind
from trustdesk.marks import Mark


class PresenceCheck:
    """Mark blank evidence missing and abstain whenever there is text to judge."""

    check_id = "presence"
    implementation_version = "1.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        return tuple(self._evaluate_item(item.coordinate, item.text) for item in evidence.items)

    @staticmethod
    def _evaluate_item(coordinate: EvidenceCoordinate, text: str | None) -> CheckFinding:
        if text is None or not text.strip():
            return CheckFinding(
                kind=OutcomeKind.DECISION,
                coordinate=coordinate,
                mark=Mark.MISSING,
                rationale="Evidence item is empty. Absence of proof, not proof of absence.",
            )
        return CheckFinding(
            kind=OutcomeKind.ABSTENTION,
            coordinate=coordinate,
            mark=None,
            rationale="Evidence item contains text, so presence alone cannot judge the claim.",
        )
