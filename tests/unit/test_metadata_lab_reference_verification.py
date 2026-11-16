"""WP5: independently verified reference fixtures (P01, P03, P08, P11, P14, P15, P16).

Offline. These tests pin the verification protocol, not the facts about books: a reviewed
case must declare where each expectation came from, a provider-derived source can never
carry more than low confidence, legitimate ambiguity stays expressible (two accepted
dates, two accepted positions), and the title forms the references print still match.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metadata_lab.cases import (
    CaseValidationError,
    SOURCE_KINDS,
    load_suite_file,
    provenance_summary,
)
from metadata_lab.checks import compute_checks, expected_facts
from metadata_lab.matcher import match_name
from metadata_lab.model import Operation, SeriesMember, SeriesRecord

QUALITY = Path(__file__).resolve().parents[2] / "metadata_lab" / "suites" / "quality"
AS_OF = "2026-11-16"
REVIEWED = {
    "p09-legacy": "p09-legacy.yaml",
    "p01-harry-potter": "p01-harry-potter.yaml",
    "p03-fourth-wing": "p03-fourth-wing.yaml",
    "p08-threshing-day": "p08-threshing-day.yaml",
    "p11-memorys-legion": "p11-memorys-legion.yaml",
    "p14-dark-tower": "p14-dark-tower.yaml",
    "p15-jack-ryan-latest": "p15-jack-ryan-latest.yaml",
    "p16-winds-of-winter": "p16-winds-of-winter.yaml",
}


def _case(case_id):
    return load_suite_file(QUALITY / REVIEWED[case_id]).cases[0]


def _entries(case):
    for operation, entry in case.expect.items():
        yield f"{case.id}.{operation}", entry
        for index, member in enumerate(entry.get("members") or []):
            yield f"{case.id}.{operation}.members[{index}]", member


@pytest.mark.parametrize("case_id", sorted(REVIEWED))
def test_every_reviewed_expectation_declares_source_kind_evidence_and_a_fresh_date(case_id):
    for where, entry in _entries(_case(case_id)):
        assert entry.get("source_kind") in SOURCE_KINDS, f"{where}: source_kind"
        assert str(entry.get("evidence") or "").strip(), f"{where}: evidence"
        assert str(entry["verified_on"]) >= "2026-11-16", f"{where}: not re-verified in WP5"


@pytest.mark.parametrize("case_id", sorted(REVIEWED))
def test_no_reviewed_expectation_rests_on_a_provider_derived_source(case_id):
    for where, entry in _entries(_case(case_id)):
        assert entry["source_kind"] != "provider_derived", where


@pytest.mark.parametrize("case_id", sorted(REVIEWED))
def test_high_confidence_needs_a_primary_or_catalogue_source(case_id):
    for where, entry in _entries(_case(case_id)):
        if entry["confidence"] == "high":
            assert entry["source_kind"] in {"primary", "catalogue"}, where


@pytest.mark.parametrize("case_id", sorted(REVIEWED))
def test_reviewed_cases_do_not_cite_providers_or_the_data_they_ingest(case_id):
    """Open Library, Wikidata and the other providers are the thing under test."""

    text = (QUALITY / REVIEWED[case_id]).read_text(encoding="utf-8").lower()
    for forbidden in ("openlibrary.org", "wikidata.org", "inventaire.io", "hardcover.app"):
        assert forbidden not in text, (case_id, forbidden)


# --- validation ----------------------------------------------------------------


def _write(tmp_path, mutate):
    document = yaml.safe_load((QUALITY / "p03-fourth-wing.yaml").read_text(encoding="utf-8"))
    mutate(document["cases"][0]["expect"]["series_for_book"])
    target = tmp_path / "case.yaml"
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def test_a_provider_derived_expectation_cannot_claim_more_than_low_confidence(tmp_path):
    def mutate(entry):
        entry["source_kind"] = "provider_derived"
        entry["confidence"] = "medium"

    with pytest.raises(CaseValidationError, match="provider_derived"):
        load_suite_file(_write(tmp_path, mutate))


def test_a_provider_derived_expectation_at_low_confidence_is_allowed_and_counted(tmp_path):
    def mutate(entry):
        entry["source_kind"] = "provider_derived"
        entry["confidence"] = "low"
        entry["upstream_overlap"] = ["inventaire"]

    suite = load_suite_file(_write(tmp_path, mutate))

    summary = provenance_summary(suite)
    assert summary["provider_derived"] == 1
    assert summary["by_source_kind"]["primary"] == 4  # five entries, one changed


def test_unknown_source_kind_and_malformed_overlap_are_rejected(tmp_path):
    with pytest.raises(CaseValidationError, match="source_kind"):
        load_suite_file(_write(tmp_path, lambda e: e.update(source_kind="wikipedia")))
    with pytest.raises(CaseValidationError, match="upstream_overlap"):
        load_suite_file(_write(tmp_path, lambda e: e.update(upstream_overlap="inventaire")))


def test_accepted_published_needs_a_published_value(tmp_path):
    document_path = _write(tmp_path, lambda e: None)
    document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
    member = document["cases"][0]["expect"]["series_members"]["members"][0]
    member["accepted_published"] = ["2026-01-01"]
    document_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(CaseValidationError, match="published"):
        load_suite_file(document_path)


def test_the_provenance_summary_flags_an_unreviewed_suite():
    reviewed = provenance_summary(load_suite_file(QUALITY / "p14-dark-tower.yaml"))
    untouched = provenance_summary(load_suite_file(QUALITY / "p02-expanse.yaml"))

    assert reviewed["undeclared"] == 0 and reviewed["provider_derived"] == 0
    assert untouched["undeclared"] == untouched["expectations"] > 0


# --- ambiguity is preserved, not forced -------------------------------------------


def _member_checks(case, members, *, name=None):
    expect = case.expect["series_members"]
    record = SeriesRecord(provider_id="p", name=name or expect["series"], members=members)
    return {
        c["name"]: c
        for c in compute_checks(
            Operation.SERIES_MEMBERS,
            expect,
            [record],
            truncated=False,
            context_terms=(expect["series"],),
            as_of=AS_OF,
        )
    }


def test_threshing_day_accepts_both_documented_release_dates_and_nothing_else():
    case = _case("p08-threshing-day")
    outcomes = {}
    for observed in ("2026-09-29", "2026-11-16", "2027-01-05"):
        member = SeriesMember(title="Threshing Day", published=observed, work_type="collection")
        outcomes[observed] = _member_checks(case, [member])["series.member[0].published"]

    assert outcomes["2026-09-29"]["outcome"] == "ok"
    assert outcomes["2026-11-16"]["outcome"] == "ok"
    assert "accepted alternative" in outcomes["2026-11-16"]["evidence"]
    assert outcomes["2027-01-05"]["outcome"] == "differs_from_reference"


def test_threshing_day_position_is_scored_on_catalogue_evidence_and_book_four_stays_forbidden():
    """OverDrive's 3.5 is separate catalogue evidence; it does not erase the author's 'no number'."""

    case = _case("p08-threshing-day")
    wanted = case.expect["series_members"]["members"][0]

    assert wanted["accepted_positions"] == ["3.5"]
    assert wanted["source_kind"] == "secondary" and wanted["confidence"] == "medium"
    assert "overdrive.com/media/13223405" in wanted["source"]
    assert any("was wrong and is corrected" in note for note in case.notes)

    def checks(position):
        member = SeriesMember(
            title="Threshing Day", position=position, work_type="collection", published="2026-09-29"
        )
        return _member_checks(case, [member])

    assert checks("3.5")["series.member[0].position"]["outcome"] == "ok"
    assert checks("3.5")["series.forbid[1]"]["outcome"] == "ok"
    assert checks("4")["series.member[0].position"]["outcome"] == "differs_from_reference"
    assert checks("4")["series.forbid[1]"]["outcome"] == "differs_from_reference"
    # No position at all: the author lists none, so a provider without one is silent, not wrong.
    assert checks(None)["series.member[0].position"]["outcome"] == "not_found"
    assert checks(None)["series.forbid[1]"]["outcome"] == "ok"


def test_ignite_is_position_075_and_070_is_a_difference():
    case = _case("p09-legacy")
    members = case.expect["series_members"]["members"]
    ignite = next(m for m in members if m["title"] == "Ignite")

    assert ignite["position"] == "0.75"
    assert not ignite.get("accepted_positions")
    assert "rebeccayarros.com/legacy" in ignite["source"] and ignite["source_kind"] == "primary"

    def outcome(position):
        listed = [
            SeriesMember(title=m["title"], position=position if m["title"] == "Ignite" else m["position"])
            for m in members
        ]
        return _member_checks(case, listed)["series.member[1].position"]["outcome"]

    assert outcome("0.75") == "ok"
    assert outcome(".75") == "ok"
    assert outcome("0.7") == "differs_from_reference", "0.7 is not equivalent to 0.75"


def test_threshing_day_retailer_title_is_an_alias_not_a_different_book():
    case = _case("p08-threshing-day")
    member = SeriesMember(title="Threshing Day (Wing and Claw Collection)", work_type="collection")

    assert _member_checks(case, [member])["series.member[0].present"]["outcome"] == "ok"


def test_book_one_matches_under_its_uk_and_us_titles():
    case = _case("p01-harry-potter")
    for title in ("Harry Potter and the Philosopher's Stone", "Harry Potter and the Sorcerer's Stone"):
        checks = _member_checks(case, [SeriesMember(title=title, position="1")])
        assert checks["series.member[0].present"]["outcome"] == "ok", title
        assert checks["series.member[0].position"]["outcome"] == "ok", title


def test_cursed_child_is_not_the_eighth_novel_but_is_not_forbidden_either():
    """Official sites call it 'the eighth story' (a stage play): ambiguity, so no forbid entry."""

    case = _case("p01-harry-potter")
    expect = case.expect["series_members"]

    assert expect["main_count"] == 7
    assert not expect.get("forbid")
    assert not match_name("Harry Potter and the Philosopher's Stone", "Harry Potter and the Cursed Child").is_match


def test_wind_through_the_keyhole_accepts_story_and_publication_order_and_the_official_label():
    case = _case("p14-dark-tower")
    wanted = case.expect["series_members"]["members"][7]

    assert set(wanted["accepted_positions"]) == {"4.5", "8"}
    for title in (
        "The Dark Tower IV 1/2: The Wind Through the Keyhole",
        "The Dark Tower IV-1/2: The Wind Through the Keyhole",
        "The Wind Through the Keyhole",
    ):
        assert match_name(wanted["title"], title, aliases=wanted["aliases"]).is_match, title
    for position in ("4.5", "8"):
        member = SeriesMember(title="The Wind Through the Keyhole", position=position)
        assert _member_checks(case, [member])["series.member[7].position"]["outcome"] == "ok", position
    odd = SeriesMember(title="The Wind Through the Keyhole", position="5")
    assert _member_checks(case, [odd])["series.member[7].position"]["outcome"] == "differs_from_reference"


def test_dark_tower_official_titles_match_and_look_alikes_do_not():
    case = _case("p14-dark-tower")
    members = case.expect["series_members"]["members"]

    for index, official in {
        0: "The Dark Tower I: The Gunslinger",
        5: "The Dark Tower VI: The Song of Susannah",
    }.items():
        wanted = members[index]
        assert match_name(wanted["title"], official, aliases=wanted["aliases"]).is_match, official
    gunslinger = members[0]
    assert not match_name(
        gunslinger["title"], "The Dark Tower: The Gunslinger Born", aliases=gunslinger["aliases"]
    ).is_match


def test_memorys_legion_is_matched_by_its_full_title_and_stays_unnumbered():
    case = _case("p11-memorys-legion")
    expect = case.expect["series_members"]
    member = SeriesMember(
        title="Memory's Legion: The Complete Expanse Story Collection", work_type="collection"
    )

    checks = _member_checks(case, [member])
    assert checks["series.member[0].present"]["outcome"] == "ok"
    assert checks["series.forbid[0]"]["outcome"] == "ok"
    numbered = SeriesMember(title="Memory's Legion", position="10", work_type="collection")
    assert _member_checks(case, [numbered])["series.forbid[0]"]["outcome"] == "differs_from_reference"
    assert expect["forbid"][0]["position"] == "10"


def test_coldest_war_author_is_now_asserted_and_matches_both_spellings():
    case = _case("p15-jack-ryan-latest")
    wanted = case.expect["series_members"]["members"][1]

    assert wanted["authors"] == ["M. P. Woodward"]
    for spelling in ("M.P. Woodward", "M. P. Woodward"):
        member = SeriesMember(title="Tom Clancy The Coldest War", authors=[spelling], published="2026-11-24")
        checks = _member_checks(case, [member])
        assert checks["series.member[1].present"]["outcome"] == "ok", spelling
        assert checks["series.member[1].authors[0]"]["outcome"] == "ok", spelling
        assert checks["series.member[1].published"]["outcome"] == "ok", spelling


def test_the_large_print_date_is_a_difference_with_evidence_not_a_silent_pass():
    case = _case("p15-jack-ryan-latest")
    member = SeriesMember(title="The Coldest War", authors=["M. P. Woodward"], published="2026-12-15")

    assert _member_checks(case, [member])["series.member[1].published"]["outcome"] == "differs_from_reference"


def test_winds_of_winter_position_is_six_and_seven_stays_forbidden():
    case = _case("p16-winds-of-winter")
    expect = case.expect["series_members"]

    assert expect["members"][0]["position"] == "6"
    six = SeriesMember(title="The Winds of Winter", position="6")
    seven = SeriesMember(title="The Winds of Winter", position="7")
    assert _member_checks(case, [six])["series.forbid[0]"]["outcome"] == "ok"
    assert _member_checks(case, [seven])["series.forbid[0]"]["outcome"] == "differs_from_reference"


def test_fourth_wing_series_name_forms_from_the_authors_site_are_all_accepted():
    case = _case("p03-fourth-wing")
    entry = case.expect["series_for_book"]

    assert {"Empyrean", "Empyrean Series"} <= set(entry["series_aliases"])
    assert [m["position"] for m in case.expect["series_members"]["members"]] == ["1", "2", "3"]
