"""The verdict rule. Derived from marks, never written by a model."""

import pytest

from trustdesk.marks import Mark, Verdict, derive_verdict, reduce_field

SUP, SIL, MIS, CON, FAI = (
    Mark.SUPPORTS,
    Mark.SILENT,
    Mark.MISSING,
    Mark.CONFLICTS,
    Mark.FAILED,
)


@pytest.mark.parametrize(
    ("marks", "expected", "why"),
    [
        ([SUP, SUP, SUP, SUP], Verdict.STRONG_SUPPORT, "all four agree"),
        ([SUP, SUP, SUP, SIL], Verdict.STRONG_SUPPORT, "three support, one silent"),
        ([SUP, SUP, MIS, SUP], Verdict.STRONG_SUPPORT, "three support, one field absent"),
        ([SIL, SUP, SIL, SIL], Verdict.LIMITED_SUPPORT, "claimed once, nothing corroborates"),
        ([CON, SUP, SIL, SIL], Verdict.CONFLICTING, "a contradiction outranks any support"),
        ([SIL, SUP, MIS, MIS], Verdict.NOT_ENOUGH_DATA, "only two fields populated"),
        ([MIS, SUP, MIS, MIS], Verdict.NOT_ENOUGH_DATA, "only the claim itself is present"),
        ([MIS, MIS, MIS, MIS], Verdict.NOT_ENOUGH_DATA, "empty record"),
        ([SUP, SUP, FAI, SUP], Verdict.COULD_NOT_CHECK, "one unreadable field"),
        ([CON, FAI, MIS, MIS], Verdict.CONFLICTING, "conflict remains more informative"),
    ],
)
def test_derive_verdict(marks, expected, why):
    assert derive_verdict(marks) is expected, why


def test_contradiction_wins_even_when_data_is_sparse():
    """Rule 1 precedes rule 2 on purpose: a contradiction is informative on its own.

    A single field that refutes the claim tells us more than three empty ones hide.
    """
    assert derive_verdict([CON, MIS, MIS, MIS]) is Verdict.CONFLICTING


def test_quarantine_precedes_evidence_reduction():
    assert (
        derive_verdict([SUP, SUP, SUP, SUP], quarantined=True)
        is Verdict.COULD_NOT_CHECK
    )


def test_silent_and_missing_are_never_interchangeable():
    """The data-desert versus medical-desert distinction, at field level.

    Same shape, one letter apart in the data, completely different meaning to a planner.
    """
    populated = derive_verdict([SIL, SUP, SIL, SIL])
    sparse = derive_verdict([MIS, SUP, MIS, MIS])
    assert populated is Verdict.LIMITED_SUPPORT
    assert sparse is Verdict.NOT_ENOUGH_DATA


def test_rejects_wrong_field_count():
    with pytest.raises(ValueError, match="exactly 4"):
        derive_verdict([SUP, SUP, SUP])


@pytest.mark.parametrize(
    ("item_marks", "unresolved", "processing_failures", "expected"),
    [
        ([SUP, MIS], 0, 0, SUP),
        ([SUP, CON], 0, 0, CON),
        ([SUP], 1, 0, SUP),
        ([], 1, 0, None),
        ([SUP], 0, 1, FAI),
        ([MIS, MIS], 0, 0, MIS),
        ([MIS, SIL], 0, 0, SIL),
    ],
)
def test_reduce_field_is_conservative_about_mixed_and_unresolved_items(
    item_marks,
    unresolved,
    processing_failures,
    expected,
):
    assert (
        reduce_field(
            item_marks,
            unresolved=unresolved,
            processing_failures=processing_failures,
        )
        is expected
    )
