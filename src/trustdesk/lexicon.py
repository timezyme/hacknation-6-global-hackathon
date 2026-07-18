"""Surface forms for the six capabilities, and the language that refutes a claim.

Everything here is a tuning surface, not settled truth. The term lists were written against the
concept mock's fixtures and must be revised once the real `description` text is in hand — see
`docs/verdict-contract.md`, open questions 6 and 7.

Over-matching a capability term is cheap: it sends the field to a higher rung or marks it as
supported, and a later rung can still disagree. Missing a term is expensive: the field goes
`silent` and nothing ever revisits it. Bias the lists toward recall.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Longer forms are matched in preference to shorter ones at the same position, so a span reported
# to the UI reads as "intensive care unit" rather than a truncated "intensive care".
CAPABILITY_TERMS: dict[str, tuple[str, ...]] = {
    "ICU": (
        "intensive care unit", "intensive care", "critical care", "intensive-care",
        "life support", "ventilator-dependent", "ventilator", "ventilated", "ICU",
    ),
    "maternity": (
        "obstetrics and gynaecology", "labour room", "labor room", "delivery room",
        "antenatal", "postnatal", "caesarean", "cesarean", "childbirth", "obstetric",
        "obstetrics", "maternity", "midwife", "midwifery", "delivery", "deliveries",
    ),
    "emergency": (
        "accident and emergency", "emergency medicine", "emergency department",
        "casualty department", "after-hours", "resuscitation", "emergencies",
        "emergency", "casualty", "triage",
    ),
    "oncology": (
        "radiotherapy", "chemotherapy", "oncology", "oncologist", "cancer",
        "tumour", "tumor", "palliative", "malignancy",
    ),
    "trauma": (
        "trauma centre", "trauma center", "fracture", "orthopaedic", "orthopedic",
        "road traffic", "resuscitation", "accident", "trauma", "injury", "injuries",
    ),
    "NICU": (
        "neonatal intensive care", "neonatology", "neonatal", "newborn", "preterm",
        "premature", "incubator", "low-birth-weight", "NICU",
    ),
}

# Language that says a service is absent, discontinued, or sent elsewhere.
#
# Deliberately does NOT include bare "referral": a "referral hospital" is one that RECEIVES
# referrals, which supports a capability claim rather than refuting it. That distinction cost a
# false positive on the Kosi Valley fixture and is regression-tested.
REFUTATION_PATTERNS: tuple[str, ...] = (
    r"\b(?:are|is|was|were|be)\s+referred\b",
    r"\breferred\s+(?:to|out|elsewhere)\b",
    r"\brefers\b[^.]{0,40}?\bto\b",
    r"\b(?:diverted|directed|transferred)\s+to\b",
    r"\bnot\s+(?:available|provided|maintained|offered|performed|equipped)\b",
    r"\bno\s+(?:\w+\s+){0,4}?(?:on\s?-?site|available|maintained|cover|facility|unit|services?)\b",
    r"\b(?:has\s+been\s+closed|closed\s+since|currently\s+closed|temporarily\s+closed)\b",
    r"\bunder\s+(?:renovation|construction|repair)\b",
    r"\bwithout\s+(?:\w+\s+){0,3}?(?:cover|facility|unit|support)\b",
    r"\bceased\b",
    r"\bdiscontinued\b",
)

_REFUTATION_RE = re.compile("|".join(REFUTATION_PATTERNS), re.IGNORECASE)


@lru_cache(maxsize=None)
def capability_pattern(capability: str) -> re.Pattern[str]:
    """Word-boundary matcher for one capability. Raises KeyError on an unknown capability.

    Failing loudly matters: a typo'd capability that quietly matched nothing would mark all 10,000
    records `silent` and look like a finding rather than a bug.
    """
    terms = CAPABILITY_TERMS[capability]
    ordered = sorted(terms, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in ordered) + r")\b", re.IGNORECASE)


def find_refutation(text: str) -> str | None:
    """Return the refuting phrase, or None. The phrase itself becomes the rationale shown to a user."""
    match = _REFUTATION_RE.search(text)
    return match.group(0) if match else None


CAPABILITIES: tuple[str, ...] = tuple(CAPABILITY_TERMS)
