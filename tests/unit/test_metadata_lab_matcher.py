"""Matcher table tests and case-loader tests.

The matcher table is deliberately full of the awkward names the plan calls out:
subtitles, punctuation, "Last, First", initials, accents, "&"/"and", trailing
"(Series, #n)" and near-misses that must NOT match.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metadata_lab.cases import CaseValidationError, load_suite_file
from metadata_lab.matcher import (
    MATCHER_VERSION,
    alternate_titles,
    compare_date,
    compare_position,
    isbn10_to_13,
    isbn_check_digit_ok,
    isbn_equal,
    match_name,
    norm_author,
    norm_isbn,
    norm_series,
    norm_title,
    parse_position,
)
from metadata_lab.model import Operation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fourth Wing", "fourth wing"),
        ("  fourth   wing ", "fourth wing"),
        ("The Empyrean", "empyrean"),
        ("A Game of Thrones", "game of thrones"),
        ("An Ember in the Ashes", "ember in the ashes"),
        ("Flight & Glory", "flight and glory"),
        ("Flight and Glory", "flight and glory"),
        ("Fourth Wing (The Empyrean, #1)", "fourth wing"),
        ("Iron Flame (The Empyrean, 2)", "iron flame"),
        ("Café Society", "cafe society"),
        ("Émile", "emile"),
        ("MURDERBOT", "murderbot"),
        ("Dune: Messiah", "dune messiah"),
        ("The The", "the"),
        ("1984", "1984"),
        ("The Empyrean (#3.5)", "empyrean"),
        ("Threshing Day (The Empyrean, #3.5)", "threshing day"),
    ],
)
def test_norm_title(raw: str, expected: str):
    assert norm_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tolkien, J.R.R.", "jrr tolkien"),
        ("J. R. R. Tolkien", "jrr tolkien"),
        ("J.R.R. Tolkien", "jrr tolkien"),
        ("Tolkien, J. R. R.", "jrr tolkien"),
        ("Rebecca Yarros", "rebecca yarros"),
        ("Yarros, Rebecca", "rebecca yarros"),
        ("RÉBECCA YARROS", "rebecca yarros"),
        ("O'Brien, Patrick", "patrick obrien"),
        ("Ursula K. Le Guin", "ursula k le guin"),
        ("  Rebecca   Yarros  ", "rebecca yarros"),
    ],
)
def test_norm_author(raw: str, expected: str):
    assert norm_author(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The Empyrean", "empyrean"),
        ("Empyrean Series", "empyrean"),
        ("The Empyrean Series", "empyrean"),
        ("The Expanse Saga", "expanse"),
        ("Mistborn Trilogy", "mistborn"),
        ("The Wheel of Time", "wheel of time"),
        ("The Murderbot Diaries", "murderbot diaries"),
    ],
)
def test_norm_series(raw: str, expected: str):
    assert norm_series(raw) == expected


@pytest.mark.parametrize(
    ("expected", "observed", "aliases", "field", "context", "kind"),
    [
        ("Fourth Wing", "Fourth Wing", (), "title", (), "exact"),
        ("Fourth Wing", "Fourth  Wing", (), "title", (), "normalized"),
        ("The Empyrean", "Empyrean", (), "series", (), "normalized"),
        ("The Empyrean", "The Empyrean Series", (), "series", (), "normalized"),
        ("Murderbot Diaries", "The Murderbot Diaries", (), "series", (), "normalized"),
        ("Tolkien, J.R.R.", "J. R. R. Tolkien", (), "author", (), "normalized"),
        ("J.R.R. Tolkien", "Tolkien, J. R. R.", (), "author", (), "normalized"),
        # Series/edition detail after the first segment is boilerplate, so the
        # series name only needs to be declared (never guessed).
        (
            "Fourth Wing",
            "Fourth Wing: The Empyrean, Book 1",
            (),
            "title",
            ("The Empyrean",),
            "subtitle",
        ),
        (
            "Fourth Wing",
            "Fourth Wing - The Empyrean Book 1",
            (),
            "title",
            ("The Empyrean",),
            "subtitle",
        ),
        # The plan defines the separators as " : ", " - " and ": ", so a comma
        # suffix is not a subtitle boundary and does not match.
        ("Fourth Wing", "Fourth Wing, Book 1", (), "title", (), "none"),
        # A trailing parenthetical is removed by the normaliser, so this is plain.
        ("Iron Flame", "Iron Flame (The Empyrean, #2)", (), "title", (), "normalized"),
        (
            "Fantastic Mr. Fox",
            "Fantastic Mister Fox",
            ("Fantastic Mister Fox",),
            "title",
            (),
            "alias",
        ),
        ("Fourth Wing", "Fourth Weng", (), "title", (), "fuzzy"),
        ("Iron Flame", "Fourth Wing", (), "title", (), "none"),
        ("Fourth Wing", "Onyx Storm", (), "title", (), "none"),
        ("The Empyrean", "The Sandman", (), "series", (), "none"),
        ("Rebecca Yarros", "Rebecca Ivers", (), "author", (), "none"),
    ],
)
def test_match_name_kinds(expected, observed, aliases, field, context, kind):
    match = match_name(expected, observed, aliases, field=field, context_terms=context)

    assert match.kind == kind
    assert match.evidence
    assert match.is_match is (kind in {"exact", "normalized", "alias", "subtitle"})


@pytest.mark.parametrize(
    ("expected", "observed", "context"),
    [
        # Shared first segment, meaningful remainder -> different works.
        ("Dune", "Dune: Messiah", ()),
        ("Dune: Messiah", "Dune: Part Two", ()),
        ("Star Wars: A New Hope", "Star Wars: The Empire Strikes Back", ()),
        ("Star Wars", "Star Wars: The Empire Strikes Back", ()),
        # Shared *middle* segment must never be enough (the old bug).
        ("Fourth Wing: The Empyrean", "Iron Flame: The Empyrean", ()),
        # The series name is only accepted when the case declares it.
        ("Fourth Wing", "Fourth Wing: The Empyrean, Book 1", ()),
    ],
)
def test_shared_segments_are_never_ok(expected, observed, context):
    match = match_name(expected, observed, field="title", context_terms=context)

    assert match.is_match is False
    assert match.kind in {"fuzzy", "none"}


def test_bare_series_prefix_is_ambiguous_not_ok():
    match = match_name("Dune", "Dune: Messiah")

    assert match.kind == "fuzzy"
    assert match.is_match is False
    assert "ambiguous" in match.evidence


def test_rejected_subtitle_candidate_explains_itself():
    match = match_name("Star Wars", "Star Wars: The Empire Strikes Back")

    assert match.kind == "fuzzy"
    assert "The Empire Strikes Back" in match.evidence


def test_fuzzy_is_never_a_match():
    match = match_name("Fourth Wing", "Fourth Weng")

    assert match.kind == "fuzzy"
    assert match.is_match is False
    assert "not a match" in match.evidence


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3.0),
        ("3.0", 3.0),
        ("3.5", 3.5),
        ("3.50", 3.5),
        ("#3.5", 3.5),
        ("book 3.5", 3.5),
        ("Book #3.5", 3.5),
        ("  2  ", 2.0),
        ("1-3", None),
        ("", None),
        ("   ", None),
        (None, None),
        ("book three", None),
        ("omnibus", None),
    ],
)
def test_parse_position(raw, expected):
    assert parse_position(raw) == expected


@pytest.mark.parametrize(
    ("expected", "observed", "equal", "note_fragment"),
    [
        ("3.5", "3.5", True, "3.5"),
        ("3.5", "3.50", True, "3.5"),
        ("3", "3.0", True, "3"),
        ("3.5", "4", False, "!="),
        ("3.5", "1-3", False, "unparseable position"),
        ("1-3", "3", False, "unparseable expected position"),
        (None, "3.5", False, "no expected position"),
        ("3.5", None, False, "unparseable position"),
    ],
)
def test_compare_position(expected, observed, equal, note_fragment):
    result = compare_position(expected, observed)

    assert result.equal is equal
    assert note_fragment in result.note


def test_matcher_version_is_recorded():
    # Bumped to "2" in Phase 4b (ISBN, alternate titles, date comparison).
    assert MATCHER_VERSION == "2"


# --- ISBN, alternate titles and dates (matcher v2) --------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("978-1-64937-404-2", "9781649374042"),
        ("1 64937 404 6", "1649374046"),
        ("164937404X", "164937404X"),
        ("", ""),
        (None, ""),
    ],
)
def test_norm_isbn(raw, expected):
    assert norm_isbn(raw) == expected


@pytest.mark.parametrize(
    ("raw", "valid"),
    [
        ("9781649374042", True),
        ("1649374046", True),
        ("9781649374040", False),
        ("1649374047", False),
        ("not-an-isbn", False),
    ],
)
def test_isbn_check_digit(raw, valid):
    assert isbn_check_digit_ok(raw) is valid


def test_isbn10_and_13_of_the_same_book_are_equal():
    # The 10 and 13 forms printed on the same Open Library edition record.
    assert isbn10_to_13("1649374046") == "9781649374042"
    assert isbn_equal("1649374046", "978-1-64937-404-2") is True
    assert isbn_equal("9781649374042", "9781649374080") is False


def test_isbn_with_a_bad_check_digit_is_never_equal():
    assert isbn_equal("9781649374040", "9781649374040") is False


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("Moby-Dick; or, The Whale", "Moby Dick"),
        ("Moby-Dick; or, The Whale", "Moby-Dick"),
        ("Moby Dick", "Moby-Dick; or, The Whale"),
    ],
)
def test_alternate_title_forms_match(name, title):
    match = match_name(title, name)

    assert match.kind == "normalized"
    assert match.is_match is True


def test_alternate_titles_do_not_loosen_other_titles():
    assert alternate_titles("Fourth Wing") == ["Fourth Wing"]
    assert match_name("Dune", "Dune Messiah").is_match is False


@pytest.mark.parametrize(
    ("expected", "observed", "equal", "precision"),
    [
        ("2021", "2021-05-04", True, "year"),
        ("2021-05", "2021-05-04", True, "month"),
        ("2021-05-04", "2021-05-04", True, "day"),
        ("2021-05-04", "2021", True, "year"),
        ("2021-05-04", "2022-05-04", False, "day"),
        ("2021-05", "2021-06-04", False, "day"),
        ("", "2021", False, "unknown"),
        ("not a date", "2021", False, "unknown"),
    ],
)
def test_compare_date(expected, observed, equal, precision):
    result = compare_date(expected, observed)

    assert result.equal is equal
    assert result.precision == precision
    assert result.note


# --- case loading ---------------------------------------------------------------

VALID = """\
suite: demo-suite
version: 1
cases:
  - id: fw
    description: "a demo case"
    reference:
      source: "publisher page to check against"
      verified_on: null
    input: {title: "Fourth Wing", isbn: "9781649374042"}
    operations: [book_by_title, book_by_isbn]
"""


def _write(tmp_path: Path, body: str, name: str = "suite.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_suite_loads(tmp_path: Path):
    suite = load_suite_file(_write(tmp_path, VALID))

    assert suite.name == "demo-suite"
    assert suite.version == 1
    assert suite.providers is None and suite.provider_label == "all"
    assert len(suite.sha256) == 64
    case = suite.case("fw")
    assert case.operations == (Operation.BOOK_BY_TITLE, Operation.BOOK_BY_ISBN)
    assert case.verified is False  # verified_on: null means UNVERIFIED
    assert case.input["isbn"] == "9781649374042"


def test_verified_date_marks_the_case_verified(tmp_path: Path):
    path = _write(tmp_path, VALID.replace("verified_on: null", "verified_on: 2026-11-14"))

    assert load_suite_file(path).case("fw").verified is True


def test_provider_list_is_parsed(tmp_path: Path):
    path = _write(tmp_path, VALID.replace("version: 1", "version: 1\nproviders: [openlibrary]"))

    assert load_suite_file(path).providers == ("openlibrary",)


def test_unknown_top_level_key_is_rejected(tmp_path: Path):
    path = _write(tmp_path, VALID + "provider: openlibrary\n")

    with pytest.raises(CaseValidationError, match="unknown key 'provider'"):
        load_suite_file(path)


def test_unknown_case_key_is_rejected_and_named(tmp_path: Path):
    path = _write(tmp_path, VALID + "    expected: {}\n")

    with pytest.raises(CaseValidationError) as info:
        load_suite_file(path)

    message = str(info.value)
    assert "unknown key 'expected'" in message
    assert "cases[0]" in message


def test_missing_source_is_rejected(tmp_path: Path):
    path = _write(tmp_path, VALID.replace('      source: "publisher page to check against"\n', ""))

    with pytest.raises(CaseValidationError, match="'source' is required"):
        load_suite_file(path)


def test_bad_verified_on_is_rejected(tmp_path: Path):
    path = _write(tmp_path, VALID.replace("verified_on: null", 'verified_on: "14/11/2026"'))

    with pytest.raises(CaseValidationError, match="not YYYY-MM-DD"):
        load_suite_file(path)


def test_duplicate_case_id_is_rejected(tmp_path: Path):
    duplicate = (
        "  - id: fw\n"
        '    reference: {source: "again"}\n'
        '    input: {title: "Fourth Wing"}\n'
        "    operations: [book_by_title]\n"
    )
    path = _write(tmp_path, VALID + duplicate)

    with pytest.raises(CaseValidationError, match="duplicate case id 'fw'"):
        load_suite_file(path)


def test_missing_input_for_an_operation_is_rejected(tmp_path: Path):
    path = _write(
        tmp_path,
        "suite: demo\n"
        "version: 1\n"
        "cases:\n"
        "  - id: members\n"
        '    reference: {source: "somewhere"}\n'
        '    input: {title: "Fourth Wing"}\n'
        "    operations: [series_members]\n",
    )

    with pytest.raises(CaseValidationError) as info:
        load_suite_file(path)

    assert "missing 'series' required by operation series_members" in str(info.value)


def test_unknown_operation_is_rejected(tmp_path: Path):
    path = _write(tmp_path, VALID.replace("book_by_title, book_by_isbn", "book_by_nothing"))

    with pytest.raises(CaseValidationError, match="unknown operation 'book_by_nothing'"):
        load_suite_file(path)


def test_expect_for_unlisted_operation_is_rejected(tmp_path: Path):
    path = _write(
        tmp_path,
        VALID + '    expect:\n      author_resolve: {name: "Rebecca Yarros"}\n',
    )

    with pytest.raises(CaseValidationError, match="not listed in operations"):
        load_suite_file(path)


def test_expect_unknown_key_is_rejected(tmp_path: Path):
    path = _write(
        tmp_path,
        VALID + '    expect:\n      book_by_title: {titel: "Fourth Wing"}\n',
    )

    with pytest.raises(CaseValidationError, match="unknown key 'titel'"):
        load_suite_file(path)


def test_expect_min_fields_are_validated(tmp_path: Path):
    path = _write(
        tmp_path,
        VALID + "    expect:\n      book_by_title: {min_fields: [publisher, nonsense]}\n",
    )

    with pytest.raises(CaseValidationError, match="unknown field\\(s\\) nonsense"):
        load_suite_file(path)


def test_bad_max_pages_is_rejected(tmp_path: Path):
    path = _write(tmp_path, VALID + "    options: {max_pages: 0}\n")

    with pytest.raises(CaseValidationError, match="must be a positive integer"):
        load_suite_file(path)


def test_sha256_tracks_file_content(tmp_path: Path):
    first = load_suite_file(_write(tmp_path, VALID, "a.yaml"))
    second = load_suite_file(_write(tmp_path, VALID.replace("demo-suite", "other"), "b.yaml"))
    again = load_suite_file(_write(tmp_path, VALID, "a.yaml"))

    assert first.sha256 != second.sha256
    assert first.sha256 == again.sha256


def test_case_round_trips_to_dict(tmp_path: Path):
    case = load_suite_file(_write(tmp_path, VALID)).case("fw")
    payload = case.to_dict()

    assert payload["operations"] == ["book_by_title", "book_by_isbn"]
    assert payload["reference"] == {
        "source": "publisher page to check against",
        "verified_on": None,
        "confidence": None,
    }
    assert payload["verified"] is False


# --- Phase 4b schema extensions (Evaluation Plan section 5) -----------------------

QUALITY_SUITE = """\
suite: quality-demo
version: 1
as_of: 2026-11-14
cases:
  - id: p08
    tier: medium
    tags: [first-six, classification]
    description: "classification case"
    reference:
      source: "author site"
      verified_on: null
      confidence: low
    input: {title: "Fourth Wing", author: "Rebecca Yarros", series: "The Empyrean"}
    operations: [series_members]
    traversal: true
    expect:
      series_members:
        series: "The Empyrean"
        exhaustive: false
        main_count: 3
        members:
          - title: "Threshing Day"
            position: "3.5"
            accepted_positions: ["3.5"]
            position_scheme: story
            work_type: collection
            published: "2026-09-29"
            fact_type: time_sensitive
            valid_until: 2026-12-31
            confidence: medium
            source: "https://example.org/series"
            verified_on: null
        forbid:
          - {title: "Threshing Day", work_type: novel}
          - {title: "Threshing Day", position: "4"}
"""


def test_quality_schema_extensions_load(tmp_path: Path):
    suite = load_suite_file(_write(tmp_path, QUALITY_SUITE))
    case = suite.cases[0]

    assert suite.as_of == "2026-11-14"
    assert case.tier == "medium"
    assert case.tags == ("first-six", "classification")
    assert case.reference.confidence == "low"
    expect = case.expect["series_members"]
    assert expect["members"][0]["work_type"] == "collection"
    assert expect["members"][0]["fact_type"] == "time_sensitive"
    assert expect["forbid"][1]["position"] == "4"
    assert expect["main_count"] == 3


def test_unknown_work_type_is_rejected(tmp_path: Path):
    path = _write(tmp_path, QUALITY_SUITE.replace("work_type: collection", "work_type: comic"))

    with pytest.raises(CaseValidationError, match="work_type"):
        load_suite_file(path)


def test_unknown_confidence_is_rejected(tmp_path: Path):
    path = _write(tmp_path, QUALITY_SUITE.replace("confidence: medium", "confidence: certain"))

    with pytest.raises(CaseValidationError, match="confidence"):
        load_suite_file(path)


def test_unknown_fact_type_is_rejected(tmp_path: Path):
    path = _write(tmp_path, QUALITY_SUITE.replace("fact_type: time_sensitive", "fact_type: maybe"))

    with pytest.raises(CaseValidationError, match="fact_type"):
        load_suite_file(path)


def test_forbid_entry_needs_a_title(tmp_path: Path):
    path = _write(tmp_path, QUALITY_SUITE.replace('{title: "Threshing Day", position: "4"}', "{position: '4'}"))

    with pytest.raises(CaseValidationError, match="'title' is required"):
        load_suite_file(path)


def test_bad_tier_is_rejected(tmp_path: Path):
    path = _write(tmp_path, QUALITY_SUITE.replace("tier: medium", "tier: extreme"))

    with pytest.raises(CaseValidationError, match="tier"):
        load_suite_file(path)


def test_existing_suite_still_validates_without_as_of(tmp_path: Path):
    suite = load_suite_file(_write(tmp_path, VALID))

    assert suite.as_of is None
    assert suite.cases[0].tags == ()
    assert suite.cases[0].tier is None
