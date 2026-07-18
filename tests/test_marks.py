"""The verdict rule. Derived from marks, never written by a model."""

import pytest

from trustdesk.marks import Mark, Verdict, derive_verdict

SUP, SIL, MIS, CON = Mark.SUPPORTS, Mark.SILENT, Mark.MISSING, Mark.CONFLICTS


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
    ],
)
def test_derive_verdict(marks, expected, why):
    assert derive_verdict(marks) is expected, why


def test_contradiction_wins_even_when_data_is_sparse():
    """Rule 1 precedes rule 2 on purpose: a contradiction is informative on its own.

    A single field that refutes the claim tells us more than three empty ones hide.
    """
    assert derive_verdict([CON, MIS, MIS, MIS]) is Verdict.CONFLICTING


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
