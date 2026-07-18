"""Rungs 0 and 1: the cheap, explainable part of the adjudication ladder.

Sample text here is modelled on the concept mock's fixtures, which were written to look like real
Indian facility records. It is a stand-in until the dataset lands, and the thresholds are expected
to move once we see real text. The behaviour under test is the decision shape, not the tuning.
"""

import pytest

from trustdesk.ladder import assess_field
from trustdesk.marks import Mark

# --- rung 0: is there anything to read at all? -------------------------------


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t "])
def test_absent_field_is_missing_not_silent(empty):
    finding = assess_field(empty, "ICU")
    assert finding.mark is Mark.MISSING
    assert finding.rung == 0
    assert finding.span is None


# --- rung 1: does the text mention the capability? ---------------------------


def test_plain_mention_supports():
    text = (
        "Tertiary care hospital with a 12-bed intensive care unit offering "
        "ventilator support and round-the-clock critical care staffing."
    )
    finding = assess_field(text, "ICU")
    assert finding.mark is Mark.SUPPORTS
    assert finding.rung == 1
    assert text[finding.span[0] : finding.span[1]].lower() == "intensive care unit"


def test_populated_but_unrelated_text_is_silent():
    text = (
        "A 20-bed nursing home providing general medicine, outpatient "
        "consultation and routine pathology."
    )
    finding = assess_field(text, "ICU")
    assert finding.mark is Mark.SILENT
    assert finding.rung == 1


def test_abbreviation_and_long_form_both_match():
    assert assess_field("ICU, General Surgery, Cardiology", "ICU").mark is Mark.SUPPORTS
    assert assess_field("Intensive care available", "ICU").mark is Mark.SUPPORTS


def test_substring_collisions_do_not_count_as_mentions():
    """'ICU' must not match inside another word, and a referral hospital is not a refutation."""
    assert assess_field("Auricular surgery and epicural care", "ICU").mark is Mark.SILENT


# --- rung 1 escalation: the cases the cheap rungs must refuse to decide ------


def test_refutation_near_a_mention_escalates_rather_than_guessing():
    """The Mithila case. Cheap rungs can see the tension but must not resolve it."""
    text = (
        "Secondary care hospital. All critical and ventilator-dependent cases are "
        "referred to Patna as no intensive care facility is maintained on site."
    )
    finding = assess_field(text, "ICU")
    assert finding.mark is None, "must escalate, not decide"
    assert finding.escalate is True
    assert finding.rung == 1


def test_closure_language_escalates():
    text = (
        "District hospital. The maternity wing has been closed since renovation "
        "began and all expectant mothers are directed to the sub-divisional hospital."
    )
    assert assess_field(text, "maternity").escalate is True


def test_negated_service_escalates():
    text = (
        "Daytime outpatient clinic, open 9am to 5pm with no emergency or after-hours "
        "cover. Emergencies are diverted to the district hospital."
    )
    assert assess_field(text, "emergency").escalate is True


def test_receiving_referrals_is_not_a_refutation():
    """'Referral hospital' means it accepts referrals. The opposite of referring out."""
    text = (
        "Referral hospital serving the Kosi belt. Intensive care and post-operative "
        "monitoring available."
    )
    finding = assess_field(text, "ICU")
    assert finding.mark is Mark.SUPPORTS
    assert finding.escalate is False


def test_refutation_without_any_mention_stays_silent():
    """Nothing to contradict. Escalating here would burn budget on an unrelated field."""
    text = "Dental clinic. Orthodontic cases are referred to Patna."
    finding = assess_field(text, "ICU")
    assert finding.mark is Mark.SILENT
    assert finding.escalate is False


# --- every finding must be able to explain itself ---------------------------


@pytest.mark.parametrize(
    "text",
    [None, "General medicine only", "12-bed intensive care unit", "ICU cases referred to Patna"],
)
def test_every_finding_carries_a_rationale(text):
    assert assess_field(text, "ICU").rationale.strip()


def test_unknown_capability_is_rejected_loudly():
    """A typo'd capability must not silently return SILENT for all 10k rows."""
    with pytest.raises(KeyError):
        assess_field("intensive care unit", "intensive-care")
