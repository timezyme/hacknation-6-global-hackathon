"""Free literal-vocabulary check for explicit capability support."""

from __future__ import annotations

import re

from trustdesk.ladder import CheckFinding, ClaimEvidence, CostTier, EvidenceItem, OutcomeKind
from trustdesk.lexicon import capability_pattern, find_refutation
from trustdesk.marks import Mark


class VocabularyCheck:
    """Decide explicit unrefuted mentions; abstain when semantic reading could differ."""

    check_id = "vocabulary"
    implementation_version = "1.0.0"
    cost_tier = CostTier.FREE

    def evaluate(self, evidence: ClaimEvidence) -> tuple[CheckFinding, ...]:
        pattern = capability_pattern(evidence.claim.capability)
        return tuple(self._evaluate_item(item, evidence.claim.capability, pattern) for item in evidence.items)

    @staticmethod
    def _evaluate_item(item: EvidenceItem, capability: str, pattern: re.Pattern[str]) -> CheckFinding:
        text = item.text
        if text is None or not text.strip():
            return CheckFinding(
                kind=OutcomeKind.ABSTENTION,
                coordinate=item.coordinate,
                mark=None,
                rationale="Evidence item is empty; the presence check owns that decision.",
            )

        mention = pattern.search(text)
        if mention is None:
            return CheckFinding(
                kind=OutcomeKind.ABSTENTION,
                coordinate=item.coordinate,
                mark=None,
                rationale=f"No literal {capability} mention; semantic interpretation could overturn a non-match.",
            )

        span = (mention.start(), mention.end())
        refutation = find_refutation(text)
        if refutation is not None:
            return CheckFinding(
                kind=OutcomeKind.ABSTENTION,
                coordinate=item.coordinate,
                mark=None,
                rationale=(
                    f'Mentions {capability} at "{mention.group(0)}" but also contains '
                    f'refuting language "{refutation}".'
                ),
                span=span,
            )
        return CheckFinding(
            kind=OutcomeKind.DECISION,
            coordinate=item.coordinate,
            mark=Mark.SUPPORTS,
            rationale=f'Mentions {capability} at "{mention.group(0)}", with no refuting language.',
            span=span,
        )
