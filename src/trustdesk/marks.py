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
    FAILED = "failed"


class Verdict(StrEnum):
    """The state of one claim across all four fields."""

    STRONG_SUPPORT = "strong_support"
    LIMITED_SUPPORT = "limited_support"
    CONFLICTING = "conflicting"
    COULD_NOT_CHECK = "could_not_check"
    NOT_ENOUGH_DATA = "not_enough_data"


def reduce_field(
    item_marks: Sequence[Mark],
    *,
    unresolved: int = 0,
    processing_failures: int = 0,
) -> Mark | None:
    """Conservatively reduce item outcomes without calling uncertainty silence."""
    marks = tuple(item_marks)
    if unresolved < 0 or processing_failures < 0:
        raise ValueError("item outcome counts cannot be negative")
    if Mark.CONFLICTS in marks:
        return Mark.CONFLICTS
    if Mark.FAILED in marks or processing_failures:
        return Mark.FAILED
    if Mark.SUPPORTS in marks:
        return Mark.SUPPORTS
    if unresolved:
        return None
    if marks and all(mark is Mark.MISSING for mark in marks):
        return Mark.MISSING
    if Mark.SILENT in marks:
        return Mark.SILENT
    raise ValueError("field has no item outcomes")


def derive_verdict(
    marks: Sequence[Mark | None],
    *,
    quarantined: bool = False,
) -> Verdict:
    """Reduce four field marks to one verdict.

    Order is load-bearing. A contradiction is checked before sparseness because a single refuting
    field tells a planner more than three empty ones conceal.
    """
    marks = tuple(marks)
    if len(marks) != len(FIELDS):
        raise ValueError(f"expected exactly 4 marks, one per field, got {len(marks)}")

    if quarantined:
        return Verdict.COULD_NOT_CHECK
    if Mark.CONFLICTS in marks:
        return Verdict.CONFLICTING
    if Mark.FAILED in marks:
        return Verdict.COULD_NOT_CHECK
    if sum(mark is not None and mark is not Mark.MISSING for mark in marks) < 3:
        return Verdict.NOT_ENOUGH_DATA
    if sum(mark is Mark.SUPPORTS for mark in marks) >= 3:
        return Verdict.STRONG_SUPPORT
    return Verdict.LIMITED_SUPPORT


VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.STRONG_SUPPORT: "Strong record support",
    Verdict.LIMITED_SUPPORT: "Limited record support",
    Verdict.CONFLICTING: "Conflicting evidence",
    Verdict.COULD_NOT_CHECK: "Could not check",
    Verdict.NOT_ENOUGH_DATA: "Not enough data",
}
