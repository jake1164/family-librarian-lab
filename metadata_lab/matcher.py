"""Deterministic name/position matching.

The report measures the providers only if the matcher is boring and repeatable:
pure functions, no I/O, no randomness, one version string recorded in every
result. If matching were sloppy we would be measuring our matcher instead of the
provider, which is the failure mode the short plan calls out.

Only ``exact``, ``normalized``, ``alias`` and ``subtitle`` count as a match.
A fuzzy-only hit is reported as ambiguous, never as ok.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

MATCHER_VERSION = "2"
FUZZY_THRESHOLD = 0.88

#: Match kinds that a human may treat as "the same thing".
STRONG_MATCH_KINDS = frozenset({"exact", "normalized", "alias", "subtitle"})

_ARTICLES = ("the ", "a ", "an ")
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_WHITESPACE = re.compile(r"\s+")
_SERIES_TAIL_WORDS = ("series", "saga", "trilogy", "cycle")
_SUBTITLE_SEPARATORS = (" : ", " - ", ": ", " -")
_INITIAL_THEN_INITIAL = re.compile(r"\b([a-z]) (?=[a-z]\b)")
_RANGE = re.compile(r"\d\s*-\s*\d")
_POSITION_PREFIX = re.compile(r"^(?:book|vol|volume|part|no)\.?\s+")

#: Alternate-title markers: "Moby-Dick; or, The Whale".
_ALT_TITLE_SEPARATORS = ("; or,", "; or ", " or, ")
_DATE_PATTERN = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")

#: Words that can sit in the leftover of a title without identifying a different
#: work: "Book 1", "Vol. 2", "#3", "a novel", "Special Edition".
_BOILERPLATE_KEYWORDS = frozenset({"book", "vol", "volume", "part", "no"})
_BOILERPLATE_WORDS = frozenset({"novel", "novels", "novella", "novellas"})
_EDITION_WORDS = frozenset({"special", "illustrated", "anniversary", "collectors", "edition"})
_REMAINDER_SPLIT = re.compile(r"[,;]")


@dataclass(frozen=True)
class Match:
    """How two names related, plus the evidence a human needs to see why."""

    kind: str
    evidence: str

    @property
    def is_match(self) -> bool:
        return self.kind in STRONG_MATCH_KINDS


@dataclass(frozen=True)
class PositionResult:
    equal: bool
    note: str


@dataclass(frozen=True)
class DateResult:
    """Precision-aware date comparison: ``2021`` agrees with ``2021-05-04``."""

    equal: bool
    note: str
    precision: str  # "year" | "month" | "day" | "unknown"


def norm_isbn(value: object) -> str:
    """Strip separators and upper-case the check digit; no validation here."""

    text = str(value or "").upper()
    return "".join(char for char in text if char.isdigit() or char == "X")


def isbn_check_digit_ok(value: object) -> bool:
    """Validate an ISBN-10 or ISBN-13 check digit."""

    isbn = norm_isbn(value)
    if len(isbn) == 10:
        if not isbn[:9].isdigit() or not (isbn[9].isdigit() or isbn[9] == "X"):
            return False
        total = sum((10 - index) * (10 if char == "X" else int(char)) for index, char in enumerate(isbn))
        return total % 11 == 0
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(
            (1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(isbn)
        )
        return total % 10 == 0
    return False


def isbn10_to_13(value: object) -> str | None:
    """Convert a valid ISBN-10 to its ISBN-13 form, or ``None``."""

    isbn = norm_isbn(value)
    if len(isbn) != 10 or not isbn_check_digit_ok(isbn):
        return None
    base = "978" + isbn[:9]
    total = sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(base))
    return base + str((10 - total % 10) % 10)


def isbn_equal(first: object, second: object) -> bool:
    """True when two ISBNs identify the same book (10 vs 13 included).

    A bad check digit is never a match: it is reported instead, because a typo
    in a fixture must not be scored as a provider difference.
    """

    left, right = norm_isbn(first), norm_isbn(second)
    if not left or not right:
        return False
    if not isbn_check_digit_ok(left) or not isbn_check_digit_ok(right):
        return False
    if len(left) == 10 and len(right) == 13:
        left = isbn10_to_13(left) or left
    elif len(right) == 10 and len(left) == 13:
        right = isbn10_to_13(right) or right
    return left == right


def alternate_titles(value: str) -> list[str]:
    """Split ``"Moby-Dick; or, The Whale"`` into its title forms."""

    text = str(value)
    parts = [text.strip()]
    for separator in _ALT_TITLE_SEPARATORS:
        if separator in text:
            parts.extend(piece.strip() for piece in text.split(separator))
    return [part for part in dict.fromkeys(parts) if part]


def _date_parts(value: object) -> tuple[int, ...] | None:
    match = _DATE_PATTERN.match(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def compare_date(expected: object, observed: object) -> DateResult:
    """Compare ISO partial dates, agreeing on the shorter precision."""

    left = _date_parts(expected)
    right = _date_parts(observed)
    if left is None or right is None:
        return DateResult(False, f"unparseable date {expected!r} vs {observed!r}", "unknown")
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if longer[: len(shorter)] == shorter:
        precision = {1: "year", 2: "month", 3: "day"}[len(shorter)]
        return DateResult(
            True,
            f"date agrees at {precision} precision ({expected} vs {observed})",
            precision,
        )
    return DateResult(False, f"date differs ({expected} vs {observed})", "day")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def norm_title(value: str) -> str:
    """Normalise a title (also the base for series names)."""

    text = _strip_accents(str(value)).casefold()
    text = text.replace("&", " and ")
    text = _TRAILING_PARENTHETICAL.sub("", text)
    text = _NON_ALNUM.sub(" ", text)
    text = _collapse(text)
    for article in _ARTICLES:
        if text.startswith(article):
            return text[len(article) :]
    return text


def norm_author(value: str) -> str:
    """Normalise an author name, handling "Last, First" and initials."""

    text = _strip_accents(str(value)).casefold()
    if text.count(",") == 1:
        last, first = text.split(",", 1)
        text = f"{first} {last}"
    text = text.replace("'", "").replace("\u2019", "")
    text = text.replace(".", " ")
    text = _NON_ALNUM.sub(" ", text)
    text = _collapse(text)
    while True:
        joined = _INITIAL_THEN_INITIAL.sub(r"\1", text)
        if joined == text:
            break
        text = joined
    return text


def norm_series(value: str) -> str:
    """Normalise a series name and drop a trailing "Series"/"Saga"/… word."""

    text = norm_title(value)
    for word in _SERIES_TAIL_WORDS:
        if text.endswith(f" {word}"):
            return text[: -len(word) - 1].strip()
    return text


def _normalise(value: str, field: str) -> str:
    if field == "author":
        return norm_author(value)
    if field == "series":
        return norm_series(value)
    return norm_title(value)


def _split_segments(value: str) -> tuple[str, str | None]:
    """Split a title at its first subtitle separator: ``(first, remainder|None)``."""

    text = str(value)
    best_index: int | None = None
    best_separator = ""
    for separator in _SUBTITLE_SEPARATORS:
        index = text.find(separator)
        if index != -1 and (best_index is None or index < best_index):
            best_index = index
            best_separator = separator
    if best_index is None:
        return text.strip(), None
    first = text[:best_index].strip()
    remainder = text[best_index + len(best_separator) :].strip()
    return first, (remainder or None)


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _remove_phrase(tokens: list[str], phrase: list[str]) -> list[str]:
    """Remove the first contiguous occurrence of ``phrase`` from ``tokens``."""

    if not phrase:
        return tokens
    width = len(phrase)
    for start in range(len(tokens) - width + 1):
        if tokens[start : start + width] == phrase:
            return tokens[:start] + tokens[start + width :]
    return tokens


def _remainder_is_boilerplate(remainder: str, context_terms: tuple[str, ...]) -> bool:
    """True when the leftover only says "book N / #N / a novel / edition / series".

    Anything else means the two names describe different works, so a shared
    prefix must not be reported as a subtitle match.
    """

    context = [norm_title(term).split() for term in context_terms if str(term).strip()]
    for phrase in _REMAINDER_SPLIT.split(remainder):
        if not phrase.strip():
            continue
        tokens = norm_title(phrase).split()
        for term_tokens in context:
            tokens = _remove_phrase(tokens, term_tokens)
        tokens = [
            token
            for token in tokens
            if not _is_number(token)
            and token not in _BOILERPLATE_KEYWORDS
            and token not in _BOILERPLATE_WORDS
            and token not in _EDITION_WORDS
        ]
        if tokens:
            return False
    return True


def _subtitle_match(
    expected_norm: str,
    observed_norm: str,
    expected_raw: str,
    observed_raw: str,
    field: str,
    context_terms: tuple[str, ...],
) -> Match | None:
    """Apply the first-segment rule; ``None`` means "no segment relationship"."""

    expected_first, expected_rest = _split_segments(expected_raw)
    observed_first, observed_rest = _split_segments(observed_raw)

    if observed_rest is not None and expected_norm == _normalise(observed_first, field):
        remainder = observed_rest
    elif expected_rest is not None and observed_norm == _normalise(expected_first, field):
        remainder = expected_rest
    else:
        return None

    if _remainder_is_boilerplate(remainder, context_terms):
        return Match(
            "subtitle",
            f'subtitle: "{observed_raw}" adds only series/edition detail to "{expected_raw}"',
        )
    # A shared first segment with a meaningful remainder is exactly the case a
    # naive matcher gets wrong ("Dune" vs "Dune: Messiah"), so it is ambiguous.
    return Match(
        "fuzzy",
        f'ambiguous: "{expected_raw}" and "{observed_raw}" share a prefix but '
        f'"{remainder}" is not series/edition detail (not a match)',
    )


def parse_position(value: object) -> float | None:
    """Parse a series position, returning ``None`` for ranges and prose.

    Positions are stored as strings so ``"3.5"`` and ``"1-3"`` survive; only the
    comparison parses them.
    """

    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.lstrip("#").strip()
    text = _POSITION_PREFIX.sub("", text)
    text = text.lstrip("#").strip()
    if _RANGE.search(text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compare_position(expected: object, observed: object) -> PositionResult:
    """Compare two positions numerically when both parse."""

    if expected is None or str(expected).strip() == "":
        return PositionResult(False, "no expected position")
    expected_value = parse_position(expected)
    if expected_value is None:
        return PositionResult(False, f"unparseable expected position {expected!r}")
    observed_value = parse_position(observed)
    if observed_value is None:
        return PositionResult(False, f"unparseable position {observed!r}")
    if expected_value == observed_value:
        return PositionResult(True, f"position {expected_value:g} == {observed_value:g}")
    return PositionResult(False, f"position {expected_value:g} != {observed_value:g}")


def match_name(
    expected: str,
    observed: str,
    aliases: tuple[str, ...] | list[str] = (),
    field: str = "title",
    context_terms: tuple[str, ...] | list[str] = (),
) -> Match:
    """Compare one expected name with one observed name.

    ``field`` selects the normaliser: ``"title"``, ``"author"`` or ``"series"``.
    ``context_terms`` may carry the case's own series name so that
    ``"Fourth Wing"`` still matches ``"Fourth Wing: The Empyrean, Book 1"``
    without letting any shared phrase count as a match.
    """

    expected_raw = str(expected)
    observed_raw = str(observed)

    if expected_raw.casefold().strip() == observed_raw.casefold().strip():
        return Match("exact", f'exact: "{expected_raw}"')

    expected_norm = _normalise(expected_raw, field)
    observed_norm = _normalise(observed_raw, field)
    if expected_norm and expected_norm == observed_norm:
        return Match("normalized", f'normalized: "{expected_norm}" == "{observed_norm}"')

    # "Moby-Dick; or, The Whale" must match "Moby Dick" and "Moby-Dick".
    for variant in alternate_titles(observed_raw):
        if variant != observed_raw and expected_norm == _normalise(variant, field):
            return Match(
                "normalized", f'alternate title: "{variant}" matches "{expected_raw}"'
            )
    for variant in alternate_titles(expected_raw):
        if variant != expected_raw and observed_norm == _normalise(variant, field):
            return Match(
                "normalized", f'alternate title: "{variant}" matches "{observed_raw}"'
            )

    for alias in aliases or ():
        alias_norm = _normalise(str(alias), field)
        if alias_norm and alias_norm == observed_norm:
            return Match("alias", f'alias: "{alias}" matches "{observed_raw}"')

    terms = tuple(str(term) for term in context_terms or ())
    subtitle = _subtitle_match(
        expected_norm, observed_norm, expected_raw, observed_raw, field, terms
    )
    if subtitle is not None:
        return subtitle

    ratio = difflib.SequenceMatcher(None, expected_norm, observed_norm).ratio()
    if ratio >= FUZZY_THRESHOLD:
        return Match(
            "fuzzy",
            f'fuzzy: "{observed_raw}" is {ratio:.2f} similar to "{expected_raw}" (not a match)',
        )
    return Match("none", f'no match: "{expected_raw}" vs "{observed_raw}"')


__all__ = [
    "DateResult",
    "FUZZY_THRESHOLD",
    "MATCHER_VERSION",
    "Match",
    "PositionResult",
    "alternate_titles",
    "compare_date",
    "compare_position",
    "isbn10_to_13",
    "isbn_check_digit_ok",
    "isbn_equal",
    "match_name",
    "norm_author",
    "norm_isbn",
    "norm_series",
    "norm_title",
    "parse_position",
]
