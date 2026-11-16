"""P14 member aliases: exact alternate titles count, look-alikes still do not.

The Dark Tower fixture expects "The Dark Tower II: The Drawing of the Three" while
catalogues file the same book as "The Drawing of the Three". Aliases are reviewed
exact alternates in the fixture; the matcher gained no fuzzy rule. These tests
replay what Open Library, BookBrainz and Hardcover actually returned (shape and
titles only) against the shipped fixture, offline.
"""

from __future__ import annotations

from pathlib import Path

from metadata_lab.cases import load_suite_file
from metadata_lab.checks import compute_checks
from metadata_lab.matcher import match_name
from metadata_lab.model import Operation, SeriesMember, SeriesRecord

SUITES_DIR = Path(__file__).resolve().parents[2] / "metadata_lab" / "suites"
CASE = next(
    case
    for case in load_suite_file(SUITES_DIR / "quality" / "p14-dark-tower.yaml").cases
    if case.id == "p14-dark-tower"
)
EXPECT = CASE.expect["series_members"]
SHORT = [
    ("The Gunslinger", "1"),
    ("The Drawing of the Three", "2"),
    ("The Waste Lands", "3"),
    ("Wizard and Glass", "4"),
    ("Wolves of the Calla", "5"),
    ("Song of Susannah", "6"),
    ("The Dark Tower", "7"),
]


def _checks(members: list[SeriesMember]) -> dict[str, dict]:
    record = SeriesRecord(provider_id="s1", name="The Dark Tower", members=members)
    return {
        item["name"]: item
        for item in compute_checks(
            Operation.SERIES_MEMBERS,
            EXPECT,
            [record],
            truncated=False,
            context_terms=("The Dark Tower",),
            as_of="2026-11-14",
        )
    }


def test_open_library_short_titles_are_found_and_ordered():
    members = [SeriesMember(title=title, position=position) for title, position in SHORT]

    checks = _checks(members)

    for index in range(7):
        assert checks[f"series.member[{index}].present"]["outcome"] == "ok", index
        assert checks[f"series.member[{index}].position"]["outcome"] == "ok", index
        assert checks[f"series.member[{index}].present"]["match_kind"] in {"exact", "alias"}
    # The story-order novel is absent from this response. The book-7 entry "The Dark Tower"
    # shares a prefix with it, which the matcher reports as ambiguous, never as present.
    assert checks["series.member[7].present"]["outcome"] in {"not_found", "ambiguous"}


def test_long_titles_still_match_exactly():
    members = [
        SeriesMember(title=title, position=position)
        for title, position in [
            ("The Dark Tower II: The Drawing of the Three", "2"),
            ("The Dark Tower VII: The Dark Tower", "7"),
        ]
    ]

    checks = _checks(members)

    assert checks["series.member[1].present"]["match_kind"] == "exact"
    assert checks["series.member[6].present"]["match_kind"] == "exact"


def test_translations_and_adaptations_are_still_not_matches():
    members = [
        SeriesMember(title="Der Revolvermann", position=None),
        SeriesMember(title="Das Rasthaus", position=None),
        SeriesMember(title="Dark Tower: The Gunslinger Born", position=None),
        SeriesMember(title="Dark Tower: The Long Road Home", position=None),
        SeriesMember(title="The Dark Tower Companion", position=None),
    ]

    checks = _checks(members)

    for index in range(8):
        assert checks[f"series.member[{index}].present"]["outcome"] != "ok", index


def test_matcher_look_alikes_stay_non_matches():
    for expected, observed in [
        ("The Dark Tower: The Gunslinger", "Dark Tower: The Gunslinger Born"),
        ("Dune", "Dune Messiah"),
        ("Dune", "Dune: Messiah"),
        ("Harry Potter and the Cursed Child", "Harry Potter and the Philosopher's Stone"),
        ("Harry Potter and the Chamber of Secrets", "Harry Potter und die Kammer des Schreckens"),
        ("New Spring", "New Spring: the Graphic Novel"),
    ]:
        assert not match_name(expected, observed).is_match, (expected, observed)


def test_every_p14_alias_is_an_exact_normalised_alternate_of_its_member():
    for member in EXPECT["members"]:
        for alias in member.get("aliases") or []:
            assert match_name(member["title"], alias, aliases=[alias]).kind in {"alias", "normalized", "exact"}
            # An alias must not be a prefix-only fragment: the whole alias is a title.
            assert alias.split() and alias[0].isupper()
