"""Rungs 0 and 1 of the adjudication ladder: the part that costs nothing and explains itself.

The ladder's contract is that the first rung able to decide, decides, and a rung that cannot decide
says so rather than guessing. `FieldFinding.mark is None` means escalate. It is never a failure
state and must never be rendered as an absence.

Rungs 2 and above (retrieval, LLM entailment, referee) consume only the findings this module
escalates. If that escalation rate is high on the real dataset, the ladder's economics do not work
and we will know early — that number is the point of measuring it.
"""

from __future__ import annotations

from dataclasses import dataclass

from trustdesk.lexicon import capability_pattern, find_refutation
from trustdesk.marks import Mark

RUNG_PRESENCE = 0
RUNG_LEXICAL = 1


@dataclass(frozen=True)
class FieldFinding:
    """One field, judged against one claim, by the cheapest rung that could reach a conclusion."""

    mark: Mark | None
    rung: int
    rationale: str
    span: tuple[int, int] | None = None

    @property
    def escalate(self) -> bool:
        """True when the cheap rungs saw enough to be suspicious but not enough to decide."""
        return self.mark is None


def assess_field(text: str | None, capability: str) -> FieldFinding:
    """Judge one field's text against one capability claim.

    Four outcomes, in the order they are checked:

    - the field is empty              -> MISSING, rung 0
    - it never mentions the claim     -> SILENT, rung 1
    - it mentions it, nothing refutes -> SUPPORTS, rung 1
    - it mentions it AND refutes it   -> escalate, rung 1 declines to decide
    """
    pattern = capability_pattern(capability)  # KeyError on an unknown capability, by design

    if text is None or not text.strip():
        return FieldFinding(
            mark=Mark.MISSING,
            rung=RUNG_PRESENCE,
            rationale="Field is empty. Absence of proof, not proof of absence.",
        )

    mention = pattern.search(text)
    if mention is None:
        return FieldFinding(
            mark=Mark.SILENT,
            rung=RUNG_LEXICAL,
            rationale=f"Field has content but never mentions {capability}.",
        )

    span = (mention.start(), mention.end())
    refutation = find_refutation(text)
    if refutation is not None:
        return FieldFinding(
            mark=None,
            rung=RUNG_LEXICAL,
            rationale=(
                f'Mentions {capability} at "{mention.group(0)}" but also says '
                f'"{refutation}". Too close to call without reading it properly.'
            ),
            span=span,
        )

    return FieldFinding(
        mark=Mark.SUPPORTS,
        rung=RUNG_LEXICAL,
        rationale=f'Mentions {capability} at "{mention.group(0)}", with no refuting language.',
        span=span,
    )
