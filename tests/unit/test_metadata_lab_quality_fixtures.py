"""Offline checks for the P01-P20 quality fixtures (Phase 7c).

These tests never touch the network: they prove that every fixture parses under the
same validation the CLI uses, that each expectation carries provenance, and that the
``first-six`` slice contains exactly the six cases the evaluation plan names.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from metadata_lab.cases import CONFIDENCES, FACT_TYPES, TIERS, load_suite_file

SUITES_DIR = Path(__file__).resolve().parents[2] / "metadata_lab" / "suites"
QUALITY_DIR = SUITES_DIR / "quality"
FIRST_SIX_SUITE = SUITES_DIR / "quality-first-six.yaml"

#: Evaluation Plan section 4.3, in order, with the slug used for the file name.
CASE_IDS = {
    "p01-harry-potter",
    "p02-expanse",
    "p03-fourth-wing",
    "p04-dune",
    "p05-project-hail-mary",
    "p06-renegades",
    "p07-yarros-other-series",
    "p08-threshing-day",
    "p09-legacy",
    "p10-murderbot-diaries",
    "p11-memorys-legion",
    "p12-cursed-child",
    "p13-moby-dick",
    "p14-dark-tower",
    "p15-jack-ryan-latest",
    "p16-winds-of-winter",
    "p17-sunrise-on-the-reaping",
    "p18-new-spring",
    "p19-in-death",
    "p20-dune-franchise",
}

FIRST_SIX = {
    "p01-harry-potter",
    "p07-yarros-other-series",
    "p08-threshing-day",
    "p11-memorys-legion",
    "p14-dark-tower",
    "p15-jack-ryan-latest",
}

KNOWN_TAGS = {
    "first-six",
    "accepted_alternatives",
    "aliases",
    "alternate_title",
    "as_of",
    "author_series",
    "authors",
    "collection",
    "completeness",
    "count",
    "date",
    "first_published",
    "forbid",
    "format",
    "fractional_position",
    "freshness",
    "isbn",
    "pagination",
    "position",
    "position_scheme",
    "prequel",
    "record_level",
    "series_for_book",
    "subtitle",
    "traversal",
    "valid_until",
    "work_type",
}


def quality_paths() -> list[Path]:
    return sorted(QUALITY_DIR.glob("*.yaml"))


def _assert_provenance(mapping: dict, where: str) -> None:
    source = mapping.get("source")
    assert isinstance(source, str) and source.strip(), f"{where}: 'source' is required"
    assert "verified_on" in mapping, f"{where}: 'verified_on' key is required"
    verified_on = mapping["verified_on"]
    assert verified_on is None or isinstance(verified_on, (str, date)), f"{where}: bad verified_on"
    assert mapping.get("confidence") in CONFIDENCES, f"{where}: bad confidence"
    assert mapping.get("fact_type") in FACT_TYPES, f"{where}: bad fact_type"


def test_quality_directory_has_one_file_per_planned_case() -> None:
    assert quality_paths(), "metadata_lab/suites/quality/ is empty"
    suites = [load_suite_file(path) for path in quality_paths()]
    found = {case.id for suite in suites for case in suite.cases}
    assert found == CASE_IDS
    for suite in suites:
        assert len(suite.cases) == 1, f"{suite.source}: expected exactly one case"
        assert suite.as_of == "2026-11-14"


@pytest.mark.parametrize("path", quality_paths(), ids=lambda path: path.stem)
def test_every_expectation_carries_provenance(path: Path) -> None:
    suite = load_suite_file(path)
    for case in suite.cases:
        assert case.expect, f"{case.id}: no expectations"
        for operation, entry in case.expect.items():
            _assert_provenance(entry, f"{case.id}.expect.{operation}")
            for index, member in enumerate(entry.get("members") or []):
                _assert_provenance(member, f"{case.id}.expect.{operation}.members[{index}]")


@pytest.mark.parametrize("path", quality_paths(), ids=lambda path: path.stem)
def test_cases_carry_a_plan_tier_and_only_known_tags(path: Path) -> None:
    suite = load_suite_file(path)
    for case in suite.cases:
        assert case.tier in TIERS, f"{case.id}: tier {case.tier!r} is not a plan tier"
        assert case.tags, f"{case.id}: no tags"
        unknown = set(case.tags) - KNOWN_TAGS
        assert not unknown, f"{case.id}: unknown tag(s) {sorted(unknown)}"


def test_first_six_tag_selects_exactly_the_six_planned_cases() -> None:
    tagged = {
        case.id
        for path in quality_paths()
        for suite in [load_suite_file(path)]
        for case in suite.cases
        if "first-six" in case.tags
    }
    assert tagged == FIRST_SIX


def test_first_six_suite_holds_exactly_the_six_tagged_cases() -> None:
    suite = load_suite_file(FIRST_SIX_SUITE)
    assert [case.id for case in suite.cases] and {case.id for case in suite.cases} == FIRST_SIX
    # The CLI applies --tags as {wanted} & set(case.tags); the slice must not change.
    selected = {case.id for case in suite.cases if {"first-six"} & set(case.tags)}
    assert selected == FIRST_SIX


def test_first_six_copies_match_their_source_case_files() -> None:
    suite = load_suite_file(FIRST_SIX_SUITE)
    for case in suite.cases:
        source = load_suite_file(QUALITY_DIR / f"{case.id}.yaml").cases[0]
        assert case.to_dict() == source.to_dict(), (
            f"{case.id}: quality-first-six.yaml has drifted from {case.id}.yaml"
        )
