"""Dated publication-status assertions (planned, scheduled, published, unknown).

Offline. The rules under test: a status is only true on a date; a missing release date is
never read as "unpublished"; an uncertain reference reviews a contradiction instead of
scoring the provider; and a scheduled title becomes published on its release day without
editing the fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metadata_lab.cases import CaseValidationError, load_suite_file
from metadata_lab.checks import compute_checks, expected_facts
from metadata_lab.model import Operation, SeriesMember, SeriesRecord
from metadata_lab.publication import classify_date, effective_status

QUALITY = Path(__file__).resolve().parents[2] / "metadata_lab" / "suites" / "quality"
AS_OF = "2026-11-16"


# --- date classification -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2011-07-12", "published"),
        ("2026-11-16", "published"),  # released on the evaluation day counts as released
        ("2026-11-17", "scheduled"),
        ("2027", "scheduled"),
        ("2011", "published"),
        ("2026-10", "published"),
        ("2026-12", "scheduled"),
        ("2026-11", "unknown"),  # the month contains the evaluation date
        ("2026", "unknown"),  # so does the year
        ("soon", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
        ("2026-02-30", "unknown"),
    ],
)
def test_a_release_date_is_placed_before_or_after_the_evaluation_date(value, expected):
    assert classify_date(value, AS_OF) == expected


def test_without_an_evaluation_date_nothing_can_be_placed():
    assert classify_date("2011-07-12", None) == "unknown"


def test_a_scheduled_assertion_follows_the_calendar():
    assertion = {"status": "scheduled", "as_of": "2026-11-16"}

    assert effective_status(assertion, "2026-11-24", "2026-11-16")[0] == "scheduled"
    assert effective_status(assertion, "2026-11-24", "2026-11-24")[0] == "published"
    assert effective_status(assertion, "2026-11-24", "2026-12-01")[0] == "published"


def test_a_planned_assertion_does_not_drift_and_unknown_stays_unknown():
    assert effective_status({"status": "planned", "as_of": "2026-11-16"}, None, "2030-01-01")[0] == "planned"
    assert effective_status({"status": "unknown", "as_of": "2026-11-16"}, None, AS_OF)[0] == "unknown"


def test_published_asserted_later_than_the_evaluation_date_is_not_assumed():
    assertion = {"status": "published", "as_of": "2026-11-16"}

    assert effective_status(assertion, None, "2026-11-10")[0] == "unknown"
    assert effective_status(assertion, None, "2026-11-20")[0] == "published"


# --- validation ---------------------------------------------------------------------


def _write(tmp_path, assertion, member_extra=None):
    document = yaml.safe_load((QUALITY / "p16-winds-of-winter.yaml").read_text(encoding="utf-8"))
    member = document["cases"][0]["expect"]["series_members"]["members"][0]
    if assertion is None:
        member.pop("publication_status", None)
    else:
        member["publication_status"] = assertion
    member.update(member_extra or {})
    target = tmp_path / "case.yaml"
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


GOOD = {"status": "planned", "as_of": "2026-11-16", "confidence": "medium", "evidence": "no announcement"}


def test_a_well_formed_assertion_loads(tmp_path):
    suite = load_suite_file(_write(tmp_path, dict(GOOD)))

    member = suite.cases[0].expect["series_members"]["members"][0]
    assert member["publication_status"]["status"] == "planned"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": "released"}, "status"),
        ({"as_of": None}, "as_of"),
        ({"as_of": "yesterday"}, "as_of"),
        ({"evidence": ""}, "evidence"),
        ({"confidence": "certain"}, "confidence"),
        ({"source": "x"}, "source"),
    ],
)
def test_a_malformed_assertion_is_rejected(tmp_path, change, message):
    assertion = {**GOOD, **change}
    if change.get("as_of", 1) is None:
        assertion.pop("as_of")

    with pytest.raises(CaseValidationError, match=message):
        load_suite_file(_write(tmp_path, assertion))


def test_a_scheduled_assertion_needs_the_announced_release_date(tmp_path):
    scheduled = {**GOOD, "status": "scheduled"}

    with pytest.raises(CaseValidationError, match="scheduled"):
        load_suite_file(_write(tmp_path, scheduled))
    load_suite_file(_write(tmp_path, scheduled, {"published": "2026-12-01"}))


# --- scoring ------------------------------------------------------------------------


def _expect(status, confidence, *, published=None, as_of="2026-11-16"):
    member = {
        "title": "The Winds of Winter",
        "publication_status": {
            "status": status,
            "as_of": as_of,
            "confidence": confidence,
            "evidence": "test evidence",
        },
    }
    if published:
        member["published"] = published
    return {"series": "Saga", "members": [member]}


def _score(expect, observed_date, *, present=True, run_as_of=AS_OF):
    members = [
        SeriesMember(title="The Winds of Winter", published=observed_date)
        if present
        else SeriesMember(title="A Feast for Crows", published="2005-10-17")
    ]
    record = SeriesRecord(provider_id="p", name="Saga", members=members)
    checks = compute_checks(
        Operation.SERIES_MEMBERS, expect, [record], truncated=False, context_terms=("Saga",), as_of=run_as_of
    )
    return next(c for c in checks if c["name"] == "series.member[0].publication_status")


def test_a_missing_date_is_never_read_as_unpublished():
    for status, confidence in [("planned", "medium"), ("planned", "high"), ("published", "high")]:
        result = _score(_expect(status, confidence), None)

        assert result["outcome"] == "not_found", (status, confidence)
        assert "not evidence" in result["evidence"]


def test_an_absent_member_is_omitted_not_scored():
    assert _score(_expect("planned", "high"), "2011", present=False)["outcome"] == "not_found"


def test_a_medium_reference_reviews_a_contradiction_instead_of_scoring_the_provider():
    published = _score(_expect("planned", "medium"), "2011-07-12")
    scheduled = _score(_expect("planned", "medium"), "2027-03-01")

    for result in (published, scheduled):
        assert result["outcome"] == "needs_review"
        assert "medium confidence" in result["evidence"]
    assert published["observed"]["status"] == "published"
    assert scheduled["observed"]["status"] == "scheduled"


def test_a_high_reference_scores_a_contradiction_as_a_difference():
    result = _score(_expect("planned", "high"), "2011-07-12")

    assert result["outcome"] == "differs_from_reference"
    assert result["expected"]["status"] == "planned" and result["expected"]["confidence"] == "high"


def test_a_planned_reference_cannot_be_confirmed_by_a_date():
    """A provider can only supply a date, so 'planned' is never an 'ok' for date data."""

    for observed in ("2011-07-12", "2027-03-01", "2026", "soon", None):
        for confidence in ("low", "medium", "high"):
            assert _score(_expect("planned", confidence), observed)["outcome"] != "ok", (observed, confidence)


def test_agreement_scores_ok_unless_the_reference_is_low_confidence():
    scheduled = _expect("scheduled", "high", published="2026-11-24")

    assert _score(scheduled, "2026-11-24")["outcome"] == "ok"
    assert _score(_expect("scheduled", "medium", published="2026-11-24"), "2026-11-24")["outcome"] == "ok"
    low = _score(_expect("scheduled", "low", published="2026-11-24"), "2026-11-24")
    assert low["outcome"] == "needs_review" and "low" in low["evidence"]


def test_a_scheduled_reference_turns_into_published_on_the_release_day():
    expect = _expect("scheduled", "high", published="2026-11-24")

    assert _score(expect, "2026-11-24", run_as_of="2026-11-16")["outcome"] == "ok"
    assert _score(expect, "2026-11-24", run_as_of="2026-12-01")["outcome"] == "ok"
    # a provider still showing the release as a future date after the day has passed is wrong
    assert _score(expect, "2027-01-01", run_as_of="2026-12-01")["outcome"] == "differs_from_reference"


def test_an_unplaceable_date_and_an_unknown_reference_are_both_reviewed():
    assert _score(_expect("planned", "high"), "2026")["outcome"] == "needs_review"
    unknown = _score(_expect("unknown", "high"), "2011-07-12")
    assert unknown["outcome"] == "needs_review" and "cannot say" in unknown["evidence"]


def test_the_evidence_and_date_travel_with_the_check():
    result = _score(_expect("planned", "medium"), "2011-07-12")

    assert result["expected"]["evidence"] == "test evidence"
    assert result["expected"]["as_of"] == "2026-11-16"
    assert result["dimension"] == "freshness"


def test_the_fact_list_counts_the_status_as_one_freshness_fact():
    facts = expected_facts(Operation.SERIES_MEMBERS, _expect("planned", "medium"), as_of=AS_OF)

    assert {"name": "series.member[0].publication_status", "dimension": "freshness"} in facts


def test_an_expired_reference_is_still_one_reviewed_fact_and_not_a_status_check():
    expect = _expect("planned", "medium")
    expect["members"][0]["valid_until"] = "2026-12-31"

    facts = expected_facts(Operation.SERIES_MEMBERS, expect, as_of="2027-02-01")

    assert [f["name"] for f in facts if f["name"].startswith("series.member")] == ["series.member[0]"]


# --- the real P16 fixture ------------------------------------------------------------


def test_p16_asserts_planned_with_medium_confidence_and_its_evidence():
    case = load_suite_file(QUALITY / "p16-winds-of-winter.yaml").cases[0]
    member = case.expect["series_members"]["members"][0]
    assertion = member["publication_status"]

    assert (assertion["status"], assertion["as_of"], assertion["confidence"]) == ("planned", "2026-11-16", "medium")
    assert "Bantam" in assertion["evidence"] and "five published books" in assertion["evidence"]
    assert member["position"] == "6"


def test_p16_never_marks_a_provider_wrong_for_its_release_date():
    case = load_suite_file(QUALITY / "p16-winds-of-winter.yaml").cases[0]
    expect = case.expect["series_members"]
    outcomes = set()
    for date in (None, "2011-07-12", "2027-03-01", "2012"):
        member = SeriesMember(title="The Winds of Winter", position="6", published=date)
        record = SeriesRecord(provider_id="p", name="A Song of Ice and Fire", members=[member])
        checks = compute_checks(
            Operation.SERIES_MEMBERS, expect, [record], truncated=False,
            context_terms=("A Song of Ice and Fire",), as_of=AS_OF,
        )
        outcomes.add(next(c for c in checks if c["name"].endswith("publication_status"))["outcome"])

    assert outcomes <= {"not_found", "needs_review"}
