"""The two enums the whole product speaks in, and the rule that connects them.

This module is the single owner of the verdict rule. TimeZyme kept the same confidence rule in two
layers and needed a generated inventory script to keep them agreeing; we keep it in one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

FIELDS: tuple[str, ...] = ("description", "capability", "equipment", "procedure")


class Mark(StrEnum):
    """The state of one field with respect to one claim."""

    SUPPORTS = "supports"
    SILENT = "silent"
    MISSING = "missing"
    CONFLICTS = "conflicts"


class Verdict(StrEnum):
    """The state of one claim across all four fields."""

    STRONG_SUPPORT = "strong_support"
    LIMITED_SUPPORT = "limited_support"
    CONFLICTING = "conflicting"
    NOT_ENOUGH_DATA = "not_enough_data"


def derive_verdict(marks: Sequence[Mark]) -> Verdict:
    """Reduce four field marks to one verdict.

    Order is load-bearing. A contradiction is checked before sparseness because a single refuting
    field tells a planner more than three empty ones conceal.
    """
    marks = tuple(marks)
    if len(marks) != len(FIELDS):
        raise ValueError(f"expected exactly 4 marks, one per field, got {len(marks)}")

    if Mark.CONFLICTS in marks:
        return Verdict.CONFLICTING
    if sum(mark is not Mark.MISSING for mark in marks) < 3:
        return Verdict.NOT_ENOUGH_DATA
    if sum(mark is Mark.SUPPORTS for mark in marks) >= 3:
        return Verdict.STRONG_SUPPORT
    return Verdict.LIMITED_SUPPORT


VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.STRONG_SUPPORT: "Strong record support",
    Verdict.LIMITED_SUPPORT: "Limited record support",
    Verdict.CONFLICTING: "Conflicting evidence",
    Verdict.NOT_ENOUGH_DATA: "Not enough data",
}
