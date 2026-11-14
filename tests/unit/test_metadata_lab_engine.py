"""Engine tests with fake adapters and a fake HTTP client (no network)."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from metadata_lab.cases import Case, Reference, Suite
from metadata_lab.engine import build_oneoff_suite, run_oneoff, run_suite
from metadata_lab.model import (
    AuthorRecord,
    BookRecord,
    ErrorKind,
    Operation,
    Outcome,
    Pagination,
    ProviderError,
    RunMode,
    SeriesMember,
    SeriesRecord,
    SeriesRef,
)
from metadata_lab.providers.base import ProviderAdapter, Unsupported

SHA = "0" * 64


class FakeCtx:
    """Mirrors the parts of ``OpContext`` the engine reads."""

    def __init__(self, *, truncated: bool = False) -> None:
        self.notes: list[str] = []
        self.exchanges: list[Any] = []
        self.truncated = truncated
        self.pagination = Pagination()
        self.calls_used = 0
        self.unresolved: dict[str, int] = {}

    def mark_truncated(self, reason: str) -> None:
        self.truncated = True
        self.pagination.truncated = True
        self.pagination.reason = reason
        self.notes.append(f"truncated: {reason}")

    def note(self, text: str) -> None:
        self.notes.append(text)

    def note_pagination(self, page: int, *, size=None, total=None, retrieved=None) -> None:
        self.pagination.pages_fetched = max(self.pagination.pages_fetched, int(page))
        if size is not None:
            self.pagination.page_size = int(size)
        if total is not None:
            self.pagination.total_reported = int(total)
        if retrieved is not None:
            self.pagination.retrieved = int(retrieved)

    def stats(self) -> dict[str, int]:
        return {"calls": self.calls_used, "retries": 0, "throttled": 0, "elapsed_ms": 0}

    def secret_values(self) -> set[str]:
        return set()

    def unresolved_counts(self) -> dict[str, int]:
        return dict(self.unresolved)


class FakeHttp:
    def __init__(self, *, truncated: bool = False) -> None:
        self.truncated = truncated
        self.contexts: list[FakeCtx] = []

    def context(self, **_kwargs: Any) -> FakeCtx:
        ctx = FakeCtx(truncated=self.truncated)
        self.contexts.append(ctx)
        return ctx


def make_case(
    *,
    case_id: str = "c1",
    operations: tuple[Operation, ...] = (Operation.BOOK_BY_TITLE,),
    case_input: dict[str, str] | None = None,
    expect: dict[str, Any] | None = None,
    traversal: bool = False,
    verified: bool = False,
    options: dict[str, Any] | None = None,
) -> Case:
    return Case(
        id=case_id,
        description="test case",
        reference=Reference(source="test reference", verified_on="2026-11-14" if verified else None),
        input=dict(case_input or {"title": "Fourth Wing", "author": "Rebecca Yarros"}),
        operations=operations,
        traversal=traversal,
        options=dict(options or {}),
        expect=dict(expect or {}),
        verified=verified,
    )


def make_suite(*cases: Case, providers=None) -> Suite:
    return Suite(
        name="test-suite",
        version=1,
        cases=tuple(cases),
        source="memory",
        sha256=SHA,
        providers=providers,
    )


def run(suite: Suite, adapters: dict[str, type[ProviderAdapter]], http: FakeHttp | None = None, **kwargs):
    return run_suite(
        suite,
        kwargs.pop("providers", None),
        kwargs.pop("case_ids", None),
        http_factory=lambda _pid: (http or FakeHttp()),
        adapters=adapters,
        **kwargs,
    )


BOOK = BookRecord(
    provider_id="b1",
    title="Fourth Wing",
    authors=["Rebecca Yarros"],
    isbn13=["9781649374042"],
    publisher="Entangled: Red Tower Books",
    published="2023",
    pages=517,
    language="eng",
    cover=True,
    series=[SeriesRef(name="The Empyrean", position="1", provider_id="s1")],
    author_ids=["a1"],
)


# --- decision order ---------------------------------------------------------------


def test_not_configured_never_calls_the_adapter():
    calls: list[Any] = []

    class Keyed(ProviderAdapter):
        id = "keyed"
        display_name = "Keyed"
        credential_env = "MDLAB_TEST_DEFINITELY_ABSENT"

        def book_by_title(self, ctx, q):
            calls.append(q)
            return [BOOK]

    document = run(make_suite(make_case()), {"keyed": Keyed})

    row = document["cases"][0]["results"][0]
    assert row["outcome"] == "not_configured"
    assert calls == []
    assert document["providers"][0]["configured"] is False


def test_declared_unsupported_is_reported_with_citation():
    class Seriesless(ProviderAdapter):
        id = "seriesless"
        display_name = "Seriesless"
        unsupported: ClassVar[dict[Operation, Unsupported]] = {
            Operation.SERIES_MEMBERS: Unsupported(
                reason="no series endpoint", source_url="https://example.org/api"
            )
        }

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
    )
    document = run(make_suite(case), {"seriesless": Seriesless})

    row = document["cases"][0]["results"][0]
    assert row["outcome"] == "unsupported"
    assert "no series endpoint" in row["notes"][0]
    assert "https://example.org/api" in row["notes"][0]
    assert row["records"] == []


def test_missing_method_is_not_implemented():
    class Bare(ProviderAdapter):
        id = "bare"
        display_name = "Bare"

    document = run(make_suite(make_case()), {"bare": Bare})

    assert document["cases"][0]["results"][0]["outcome"] == "not_implemented"


def test_one_weak_operation_does_not_hide_the_rest():
    class AuthorOnly(ProviderAdapter):
        id = "authoronly"
        display_name = "Author Only"
        unsupported: ClassVar[dict[Operation, Unsupported]] = {
            Operation.SERIES_MEMBERS: Unsupported(
                reason="documented: no series API", source_url="https://example.org/docs"
            )
        }

        def author_works(self, ctx, q):
            return [AuthorRecord(provider_id="a1", name="Rebecca Yarros", works=["Fourth Wing"])]

    case = make_case(
        operations=(
            Operation.BOOK_BY_TITLE,
            Operation.SERIES_MEMBERS,
            Operation.AUTHOR_WORKS,
        ),
        case_input={"title": "Fourth Wing", "series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={"author_works": {"titles": ["Fourth Wing"]}},
    )
    document = run(make_suite(case), {"authoronly": AuthorOnly})
    outcomes = {row["operation"]: row["outcome"] for row in document["cases"][0]["results"]}

    assert outcomes["book_by_title"] == "not_implemented"
    assert outcomes["series_members"] == "unsupported"
    assert outcomes["author_works"] == "ok"
    checks = document["cases"][0]["results"][2]["checks"]
    assert checks[0]["name"] == "author.titles[0]"
    assert checks[0]["outcome"] == "ok"


def test_provider_error_on_one_provider_does_not_stop_the_others():
    class Broken(ProviderAdapter):
        id = "broken"
        display_name = "Broken"

        def book_by_title(self, ctx, q):
            raise ProviderError(ErrorKind.RATE_LIMITED, "HTTP 429")

    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            return [BOOK]

    document = run(make_suite(make_case()), {"broken": Broken, "working": Working})
    rows = {row["provider"]: row for row in document["cases"][0]["results"]}

    assert rows["broken"]["outcome"] == "error"
    assert rows["broken"]["error"] == {"kind": "rate_limited", "message": "HTTP 429"}
    assert rows["working"]["outcome"] == "ok"
    assert document["matrix"]["broken"]["book_by_title"]["error"] == 1
    assert document["matrix"]["working"]["book_by_title"]["ok"] == 1


def test_adapter_bug_reports_only_the_exception_type():
    class Buggy(ProviderAdapter):
        id = "buggy"
        display_name = "Buggy"

        def book_by_title(self, ctx, q):
            raise ValueError("payload that must not be echoed")

    document = run(make_suite(make_case()), {"buggy": Buggy})
    row = document["cases"][0]["results"][0]

    assert row["outcome"] == "error"
    assert row["error"]["kind"] == "parse"
    assert row["error"]["message"] == "adapter raised ValueError"
    assert "must not be echoed" not in str(row)


def test_no_records_without_expectations_is_not_found():
    class Empty(ProviderAdapter):
        id = "empty"
        display_name = "Empty"

        def book_by_title(self, ctx, q):
            return []

    document = run(make_suite(make_case()), {"empty": Empty})

    assert document["cases"][0]["results"][0]["outcome"] == "not_found"


# --- checks -----------------------------------------------------------------------


def test_truncated_missing_member_is_incomplete_not_differs():
    class Partial(ProviderAdapter):
        id = "partial"
        display_name = "Partial"

        def series_members(self, ctx, q):
            ctx.mark_truncated("stopped at page 1")
            return [SeriesRecord(provider_id="s1", name="The Empyrean", members=[])]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={"series_members": {"series": "The Empyrean", "members": [{"title": "Threshing Day"}]}},
    )
    document = run(make_suite(case), {"partial": Partial}, http=FakeHttp())
    row = document["cases"][0]["results"][0]

    checks = {check["name"]: check for check in row["checks"]}
    assert checks["series.member[0].present"]["outcome"] == "incomplete"
    assert row["outcome"] == "incomplete"
    assert row["truncated"] is True


def test_member_present_without_position_is_partial_not_not_found():
    class NoPositions(ProviderAdapter):
        id = "nopositions"
        display_name = "No Positions"

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[
                        SeriesMember(title="Fourth Wing", position=None),
                        SeriesMember(title="Threshing Day", position=None),
                    ],
                )
            ]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={
            "series_members": {
                "series": "The Empyrean",
                "members": [
                    {"title": "Fourth Wing", "position": "1"},
                    {"title": "Threshing Day", "position": "3.5"},
                ],
            }
        },
    )
    document = run(make_suite(case), {"nopositions": NoPositions})
    row = document["cases"][0]["results"][0]
    checks = {check["name"]: check for check in row["checks"]}

    assert checks["series.member[0].present"]["outcome"] == "ok"
    assert checks["series.member[0].position"]["outcome"] == "not_found"
    assert checks["series.member[0].position"]["evidence"] == "provider returned no position"
    assert checks["series.member[1].present"]["outcome"] == "ok"
    # Membership is right; positions are missing data, so never "differs".
    assert all(check["outcome"] != "differs_from_reference" for check in row["checks"])
    # Usable data came back, so the operation is partial, never "not_found".
    assert row["outcome"] == "partial"


def test_three_of_four_members_is_partial_and_tallied():
    class ThreeMembers(ProviderAdapter):
        id = "three"
        display_name = "Three Members"

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[
                        SeriesMember(title="Fourth Wing", position="1"),
                        SeriesMember(title="Iron Flame", position="2"),
                        SeriesMember(title="Onyx Storm", position="3"),
                    ],
                )
            ]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={
            "series_members": {
                "series": "The Empyrean",
                "members": [
                    {"title": "Fourth Wing", "position": "1"},
                    {"title": "Iron Flame", "position": "2"},
                    {"title": "Onyx Storm", "position": "3"},
                    {"title": "Threshing Day", "position": "3.5"},
                ],
            }
        },
    )
    document = run(make_suite(case), {"three": ThreeMembers})
    row = document["cases"][0]["results"][0]

    assert row["outcome"] == "partial"
    assert document["matrix"]["three"]["series_members"] == {"partial": 1, "total": 1}
    tallies = document["matrix_checks"]["three"]["series_members"]
    assert tallies["series.member[].present"] == {"ok": 3, "not_found": 1, "total": 4}
    # Every expected member now yields a position check, so the absent member is
    # an omission (position coverage 3/4) rather than a silently missing row.
    assert tallies["series.member[].position"] == {"ok": 3, "not_found": 1, "total": 4}


def test_no_records_with_checks_is_not_found():
    class Empty(ProviderAdapter):
        id = "emptychecks"
        display_name = "Empty"

        def series_members(self, ctx, q):
            return []

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={"series_members": {"series": "The Empyrean", "members": [{"title": "Fourth Wing"}]}},
    )
    document = run(make_suite(case), {"emptychecks": Empty})
    row = document["cases"][0]["results"][0]

    assert row["outcome"] == "not_found"
    assert document["matrix"]["emptychecks"]["series_members"] == {"not_found": 1, "total": 1}


def test_wrong_position_differs_with_the_observed_value():
    class WrongPosition(ProviderAdapter):
        id = "wrongposition"
        display_name = "Wrong Position"

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[SeriesMember(title="Threshing Day", position="2")],
                )
            ]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={"series_members": {"members": [{"title": "Threshing Day", "position": "3.5"}]}},
    )
    document = run(make_suite(case), {"wrongposition": WrongPosition})
    row = document["cases"][0]["results"][0]
    check = next(c for c in row["checks"] if c["name"] == "series.member[0].position")

    assert check["outcome"] == "differs_from_reference"
    assert check["observed"] == "2"
    assert row["outcome"] == "differs_from_reference"


def test_series_name_from_context_terms_makes_a_subtitle_match():
    class VerboseTitles(ProviderAdapter):
        id = "verbose"
        display_name = "Verbose"

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[
                        SeriesMember(title="Fourth Wing: The Empyrean, Book 1", position="1")
                    ],
                )
            ]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={"series_members": {"members": [{"title": "Fourth Wing", "position": "1"}]}},
    )
    document = run(make_suite(case), {"verbose": VerboseTitles})
    row = document["cases"][0]["results"][0]
    present = next(c for c in row["checks"] if c["name"] == "series.member[0].present")

    assert present["outcome"] == "ok"
    assert present["match_kind"] == "subtitle"


def test_fuzzy_only_match_is_ambiguous():
    class Typo(ProviderAdapter):
        id = "typo"
        display_name = "Typo"

        def book_by_title(self, ctx, q):
            return [BookRecord(provider_id="b1", title="Fourth Weng")]

    case = make_case(expect={"book_by_title": {"title": "Fourth Wing"}})
    document = run(make_suite(case), {"typo": Typo})
    row = document["cases"][0]["results"][0]

    assert row["checks"][0]["match_kind"] == "fuzzy"
    assert row["checks"][0]["outcome"] == "ambiguous"
    assert row["outcome"] == "ambiguous"


def test_min_fields_missing_is_omission_not_a_difference():
    class Thin(ProviderAdapter):
        id = "thin"
        display_name = "Thin"

        def book_by_title(self, ctx, q):
            return [BookRecord(provider_id="b1", title="Fourth Wing", authors=["Rebecca Yarros"])]

    case = make_case(
        expect={
            "book_by_title": {
                "title": "Fourth Wing",
                "min_fields": ["authors", "publisher", "pages", "language", "cover"],
            }
        }
    )
    document = run(make_suite(case), {"thin": Thin})
    row = document["cases"][0]["results"][0]
    check = next(c for c in row["checks"] if c["name"] == "book.min_fields")

    # Omission is not inaccuracy (Evaluation Plan 4.1): a missing field is
    # coverage, so the check is not_found and the op becomes partial.
    assert check["outcome"] == "not_found"
    assert check["dimension"] == "coverage"
    assert check["observed"]["missing"] == ["publisher", "pages", "language", "cover"]
    assert "omitted" in check["evidence"]
    assert row["outcome"] == "partial"


def test_unverified_fixture_flag_is_propagated():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            return [BOOK]

    verified_case = make_case(case_id="verified", verified=True)
    unverified_case = make_case(case_id="unverified", verified=False)
    document = run(make_suite(verified_case, unverified_case), {"working": Working})

    first, second = document["cases"]
    assert first["verified"] is True
    assert all(row["reference_verified"] is True for row in first["results"])
    assert second["verified"] is False
    assert all(row["reference_verified"] is False for row in second["results"])


# --- traversal --------------------------------------------------------------------


class TraversalAdapter(ProviderAdapter):
    id = "traversal"
    display_name = "Traversal"
    broken = False

    def book_by_title(self, ctx, q):
        return [BOOK]

    def series_for_book(self, ctx, q):
        if self.broken:
            return []
        return [SeriesRecord(provider_id="s1", name="The Empyrean")]

    def series_members(self, ctx, q):
        return [
            SeriesRecord(
                provider_id="s1",
                name="The Empyrean",
                members=[SeriesMember(title="Fourth Wing", position="1")],
            )
        ]

    def author_works(self, ctx, q):
        return [AuthorRecord(provider_id="a1", name="Rebecca Yarros", works=["Fourth Wing"])]

    def author_series(self, ctx, q):
        return [AuthorRecord(provider_id="a1", name="Rebecca Yarros")]


def test_traversal_runs_and_keeps_the_independent_results():
    case = make_case(
        operations=(Operation.BOOK_BY_TITLE,),
        case_input={"title": "Fourth Wing", "author": "Rebecca Yarros"},
        traversal=True,
    )
    document = run(make_suite(case), {"traversal": TraversalAdapter})
    rows = document["cases"][0]["results"]
    modes = [(row["operation"], row["mode"]) for row in rows]

    assert ("book_by_title", "independent") in modes
    assert ("book_by_title", "traversal") in modes
    traversal = {row["operation"] for row in rows if row["mode"] == "traversal"}
    assert traversal == {
        "book_by_title",
        "series_for_book",
        "series_members",
        "author_works",
        "author_series",
    }


def test_traversal_chain_break_is_noted_and_does_not_stop_author_steps():
    class BrokenSeries(TraversalAdapter):
        broken = True

    case = make_case(
        operations=(Operation.BOOK_BY_TITLE,),
        case_input={"title": "Fourth Wing", "author": "Rebecca Yarros"},
        traversal=True,
    )
    document = run(make_suite(case), {"traversal": BrokenSeries})
    traversal_rows = [
        row for row in document["cases"][0]["results"] if row["mode"] == "traversal"
    ]
    by_operation = {row["operation"]: row for row in traversal_rows}

    assert by_operation["series_for_book"]["outcome"] == "not_found"
    assert by_operation["series_members"]["outcome"] == "not_run"
    assert "series_for_book" in by_operation["series_members"]["notes"][0]
    # The author steps do not depend on the series, so they still run.
    assert by_operation["author_works"]["outcome"] != "not_run"


def test_traversal_is_opt_in_via_parameter():
    case = make_case(operations=(Operation.BOOK_BY_TITLE,), traversal=False)
    document = run(make_suite(case), {"traversal": TraversalAdapter}, traversal=True)

    assert any(row["mode"] == "traversal" for row in document["cases"][0]["results"])


# --- document / selection ---------------------------------------------------------


def test_matrix_counts_per_outcome_and_total():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            return [BOOK]

    class Empty(ProviderAdapter):
        id = "empty"
        display_name = "Empty"

        def book_by_title(self, ctx, q):
            return []

    document = run(make_suite(make_case()), {"working": Working, "empty": Empty})

    assert document["matrix"]["working"]["book_by_title"] == {"ok": 1, "total": 1}
    assert document["matrix"]["empty"]["book_by_title"] == {"not_found": 1, "total": 1}


def test_unknown_provider_is_a_usage_error():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

    with pytest.raises(ValueError, match="unknown provider"):
        run(make_suite(make_case()), {"working": Working}, providers=["nope"])


def test_unknown_case_id_is_a_usage_error():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

    with pytest.raises(ValueError, match="unknown case id"):
        run(make_suite(make_case()), {"working": Working}, case_ids=["nope"])


def test_document_carries_the_contract_fields():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            return [BOOK]

    document = run(make_suite(make_case()), {"working": Working})

    assert document["schema"] == "metadata-lab.result/1"
    assert document["run"]["mode"] == "live"
    assert document["run"]["matcher_version"] == "2"
    assert document["suite"]["name"] == "test-suite"
    provider = document["providers"][0]
    assert provider["declared"]["book_by_title"] == "supported"
    assert provider["declared"]["series_members"] == "not_implemented"
    assert provider["verified_live"] is False


def test_replay_run_mode_and_replay_of_are_recorded():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            return [BOOK]

    document = run(
        make_suite(make_case()),
        {"working": Working},
        run_mode=RunMode.REPLAY,
        replay_of="original-run-id",
    )

    assert document["run"]["mode"] == "replay"
    assert document["run"]["replay_of"] == "original-run-id"
    assert document["cases"][0]["results"][0]["mode"] == "independent"


def test_one_off_suite_has_no_expectations():
    suite = build_oneoff_suite("book", {"title": "Fourth Wing", "author": "Rebecca Yarros"})

    case = suite.cases[0]
    assert case.operations == (Operation.BOOK_BY_TITLE,)
    assert case.expect == {}
    assert case.verified is False
    assert len(suite.sha256) == 64


def test_run_oneoff_uses_the_engine():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            return [BOOK]

    document = run_oneoff(
        "book",
        {"title": "Fourth Wing"},
        ["working"],
        http_factory=lambda _pid: FakeHttp(),
        adapters={"working": Working},
    )

    assert document["suite"]["name"] == "one-off-book"
    assert document["cases"][0]["results"][0]["outcome"] == "ok"


def test_one_off_requires_its_input():
    with pytest.raises(ValueError, match="needs isbn"):
        build_oneoff_suite("isbn", {"title": "Fourth Wing"})


# --- Phase 4b: classification, ordering, freshness, agreement, duplicates ---------


class RichMembers(ProviderAdapter):
    id = "rich"
    display_name = "Rich"

    def series_members(self, ctx, q):
        ctx.note_pagination(1, size=50, total=3, retrieved=3)
        ctx.calls_used = 1
        return [
            SeriesRecord(
                provider_id="s1",
                name="The Empyrean",
                members=[
                    SeriesMember(
                        title="Fourth Wing", position="1", work_type="novel", published="2023-05-02"
                    ),
                    SeriesMember(
                        title="Threshing Day",
                        position="3.5",
                        work_type="collection",
                        published="2026-09-29",
                    ),
                    SeriesMember(
                        title="Onyx Storm",
                        position="3",
                        work_type="novel",
                        published="2025-01-21",
                    ),
                ],
            )
        ]


def test_work_type_and_position_and_date_checks():
    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={
            "series_members": {
                "series": "The Empyrean",
                "members": [
                    {
                        "title": "Threshing Day",
                        "position": "3.5",
                        "work_type": "collection",
                        "published": "2026-09-29",
                    }
                ],
            }
        },
    )
    document = run(make_suite(case), {"rich": RichMembers})
    checks = {c["name"]: c for c in document["cases"][0]["results"][0]["checks"]}

    assert checks["series.member[0].position"]["outcome"] == "ok"
    assert checks["series.member[0].work_type"]["outcome"] == "ok"
    assert checks["series.member[0].work_type"]["dimension"] == "classification"
    assert checks["series.member[0].published"]["outcome"] == "ok"
    assert checks["series.member[0].published"]["dimension"] == "freshness"


def test_forbid_entry_flags_a_forbidden_claim():
    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={
            "series_members": {
                "series": "The Empyrean",
                "forbid": [{"title": "Threshing Day", "work_type": "novel"}],
            }
        },
    )
    document = run(make_suite(case), {"rich": RichMembers})
    row = document["cases"][0]["results"][0]
    forbid = next(c for c in row["checks"] if c["name"] == "series.forbid[0]")

    assert forbid["outcome"] == "ok"  # it is a collection, not a novel
    assert row["outcome"] == "ok"

    case.expect["series_members"]["forbid"] = [{"title": "Threshing Day", "position": "4"}]
    document = run(make_suite(case), {"rich": RichMembers})
    forbid = next(
        c for c in document["cases"][0]["results"][0]["checks"] if c["name"] == "series.forbid[0]"
    )
    assert forbid["outcome"] == "ok"  # position is 3.5, not 4

    case.expect["series_members"]["forbid"] = [{"title": "Threshing Day", "position": "3.5"}]
    document = run(make_suite(case), {"rich": RichMembers})
    forbid = next(
        c for c in document["cases"][0]["results"][0]["checks"] if c["name"] == "series.forbid[0]"
    )
    assert forbid["outcome"] == "differs_from_reference"
    assert forbid["observed"][0]["attribute"] == "position"


def test_accepted_positions_agree_on_any_listed_value():
    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={
            "series_members": {
                "series": "The Empyrean",
                "members": [
                    {"title": "Threshing Day", "accepted_positions": ["4", "3.5"]}
                ],
            }
        },
    )
    document = run(make_suite(case), {"rich": RichMembers})
    position = next(
        c
        for c in document["cases"][0]["results"][0]["checks"]
        if c["name"] == "series.member[0].position"
    )

    assert position["outcome"] == "ok"
    assert "accepted" in position["evidence"]


def test_main_count_without_classification_needs_review():
    class Unclassified(ProviderAdapter):
        id = "unclassified"
        display_name = "Unclassified"

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[SeriesMember(title="Fourth Wing", position="1")],
                )
            ]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={"series_members": {"series": "The Empyrean", "main_count": 1}},
    )
    document = run(make_suite(case), {"unclassified": Unclassified})
    row = document["cases"][0]["results"][0]
    check_ = next(c for c in row["checks"] if c["name"] == "series.main_count")

    assert check_["outcome"] == "needs_review"
    assert row["outcome"] == "needs_review"
    # needs_review is excluded from the tallies denominator.
    assert document["matrix_checks"]["unclassified"]["series_members"]["series.main_count"] == {
        "needs_review": 1,
        "total": 0,
    }


def test_expired_reference_is_needs_review_never_differs():
    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={
            "series_members": {
                "series": "The Empyrean",
                "members": [
                    {
                        "title": "Threshing Day",
                        "position": "4",
                        "fact_type": "time_sensitive",
                        "valid_until": "2026-01-01",
                    }
                ],
            }
        },
    )
    document = run(make_suite(case), {"rich": RichMembers}, as_of="2026-11-14")
    row = document["cases"][0]["results"][0]
    check_ = next(c for c in row["checks"] if c["name"] == "series.member[0]")

    assert check_["outcome"] == "needs_review"
    assert "reference expired" in check_["evidence"]
    assert row["outcome"] == "needs_review"
    assert document["run"]["as_of"] == "2026-11-14"


def test_agreement_rule_makes_disagreeing_records_ambiguous():
    class TwoSeries(ProviderAdapter):
        id = "twoseries"
        display_name = "Two Series"

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[SeriesMember(title="Fourth Wing", position="1")],
                ),
                SeriesRecord(provider_id="s2", name="The Empyrean", members=[]),
            ]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={
            "series_members": {
                "series": "The Empyrean",
                "members": [{"title": "Fourth Wing", "position": "1"}],
            }
        },
    )
    document = run(make_suite(case), {"twoseries": TwoSeries})
    row = document["cases"][0]["results"][0]
    present = next(c for c in row["checks"] if c["name"] == "series.member[0].present")

    assert present["outcome"] == "ambiguous"
    assert "records disagree" in present["evidence"]
    assert "s1" in present["evidence"] and "s2" in present["evidence"]
    # Both records are retained, nothing was chosen.
    assert {record["provider_id"] for record in row["records"]} == {"s1", "s2"}


def test_duplicate_members_are_reported_and_retained():
    class Duplicates(ProviderAdapter):
        id = "dupes"
        display_name = "Duplicates"

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[
                        SeriesMember(title="Fourth Wing", position="1", provider_id="w1"),
                        SeriesMember(title="Fourth Wing", position="1", provider_id="w2"),
                    ],
                )
            ]

    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
    )
    document = run(make_suite(case), {"dupes": Duplicates})
    row = document["cases"][0]["results"][0]

    assert row["duplicates"][0]["count"] == 2
    assert row["duplicates"][0]["provider_ids"] == ["w1", "w2"]
    assert len(row["records"][0]["members"]) == 2  # retained, not dropped


def test_rows_carry_pagination_and_stats():
    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
    )
    document = run(make_suite(case), {"rich": RichMembers})
    row = document["cases"][0]["results"][0]

    assert row["pagination"]["pages_fetched"] == 1
    assert row["pagination"]["total_reported"] == 3
    assert row["pagination"]["retrieved"] == 3
    assert row["stats"]["calls"] == 1
    assert set(row["stats"]) == {"calls", "retries", "throttled", "elapsed_ms"}


def test_excluded_provider_rows_are_not_run_with_reason():
    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            return [BOOK]

    class Other(ProviderAdapter):
        id = "other"
        display_name = "Other"

        def book_by_title(self, ctx, q):
            return [BOOK]

    document = run(
        make_suite(make_case()), {"working": Working, "other": Other}, exclude=["other"]
    )
    rows = {row["provider"]: row for row in document["cases"][0]["results"]}

    assert rows["other"]["outcome"] == "not_run"
    assert rows["other"]["reason"] == "excluded"
    assert rows["working"]["outcome"] == "ok"
    assert document["run"]["exclude"] == ["other"]
    assert [p["excluded"] for p in document["providers"] if p["id"] == "other"] == [True]


def test_resume_reuses_finished_rows_and_runs_the_rest():
    calls: list[str] = []

    class Working(ProviderAdapter):
        id = "working"
        display_name = "Working"

        def book_by_title(self, ctx, q):
            calls.append("book_by_title")
            return [BOOK]

        def author_resolve(self, ctx, q):
            calls.append("author_resolve")
            return [AuthorRecord(provider_id="a1", name="Rebecca Yarros")]

    case = make_case(
        operations=(Operation.BOOK_BY_TITLE, Operation.AUTHOR_RESOLVE),
        case_input={"title": "Fourth Wing", "author": "Rebecca Yarros"},
    )
    first = run(make_suite(case), {"working": Working})
    cancelled = json.loads(json.dumps(first))
    # Pretend the second row never ran.
    cancelled["cases"][0]["results"][1]["outcome"] = "not_run"
    cancelled["cases"][0]["results"][1]["reason"] = "cancelled"
    calls.clear()

    resumed = run(make_suite(case), {"working": Working}, resume=cancelled)

    assert calls == ["author_resolve"]  # the finished row was reused, not re-run
    rows = {row["operation"]: row for row in resumed["cases"][0]["results"]}
    assert rows["book_by_title"]["resumed"] is True
    assert rows["author_resolve"].get("resumed") is None
    assert resumed["run"]["resumed_from"] == first["run"]["id"]


def test_every_check_has_an_explicit_dimension():
    """No check may rely on a default dimension (Evaluation Plan 4.1)."""

    from metadata_lab.checks import DIMENSIONS, check

    with pytest.raises(TypeError):
        check("x", Outcome.OK, "a", "a")  # type: ignore[call-arg]

    with pytest.raises(ValueError):
        check("x", Outcome.OK, "a", "a", dimension="vibes")

    produced: list[dict] = []

    class Everything(ProviderAdapter):
        id = "everything"
        display_name = "Everything"

        def book_by_title(self, ctx, q):
            return [
                BookRecord(
                    provider_id="b1",
                    title="Fourth Wing",
                    authors=["Rebecca Yarros"],
                    isbn13=["9781649374042"],
                    publisher="P",
                    published="2023-05-02",
                    pages=1,
                    language="eng",
                    cover=True,
                    series=[SeriesRef(name="The Empyrean", position="1")],
                    work_type="novel",
                    first_published="2023",
                )
            ]

        def series_members(self, ctx, q):
            return [
                SeriesRecord(
                    provider_id="s1",
                    name="The Empyrean",
                    members=[
                        SeriesMember(
                            title="Threshing Day",
                            position="3.5",
                            work_type="collection",
                            authors=["Rebecca Yarros"],
                            published="2026-09-29",
                        )
                    ],
                )
            ]

        def author_works(self, ctx, q):
            return [AuthorRecord(provider_id="a1", name="Rebecca Yarros", works=["Fourth Wing"])]

        def author_series(self, ctx, q):
            return [
                AuthorRecord(
                    provider_id="a1",
                    name="Rebecca Yarros",
                    series=[SeriesRef(name="The Empyrean")],
                )
            ]

    cases = [
        make_case(
            operations=(Operation.BOOK_BY_TITLE,),
            expect={
                "book_by_title": {
                    "title": "Fourth Wing",
                    "authors": ["Rebecca Yarros"],
                    "isbn13": "9781649374042",
                    "min_fields": ["publisher"],
                    "work_type": "novel",
                    "first_published": "2023",
                }
            },
        ),
        make_case(
            case_id="c2",
            operations=(Operation.SERIES_MEMBERS,),
            case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
            expect={
                "series_members": {
                    "series": "The Empyrean",
                    "members": [
                        {
                            "title": "Threshing Day",
                            "position": "3.5",
                            "work_type": "collection",
                            "published": "2026-09-29",
                            "authors": ["Rebecca Yarros"],
                        }
                    ],
                    "main_count": 1,
                    "forbid": [{"title": "Threshing Day", "kind": "novel"}],
                }
            },
        ),
        make_case(
            case_id="c3",
            operations=(Operation.AUTHOR_WORKS, Operation.AUTHOR_SERIES),
            case_input={"author": "Rebecca Yarros"},
            expect={
                "author_works": {"titles": ["Fourth Wing"]},
                "author_series": {"series": ["The Empyrean"]},
            },
        ),
    ]
    document = run(make_suite(*cases), {"everything": Everything})
    produced = [
        item for case in document["cases"] for row in case["results"] for item in row["checks"]
    ]

    assert produced, "the fixture produced no checks"
    for item in produced:
        assert item["dimension"] in DIMENSIONS, item


def test_measurements_block_has_dimensions_and_no_single_score():
    case = make_case(
        operations=(Operation.SERIES_MEMBERS,),
        case_input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        expect={"series_members": {"series": "The Empyrean"}},
    )
    document = run(make_suite(case), {"rich": RichMembers})
    profile = document["measurements"]["by_provider"]["rich"]

    assert set(profile) == {
        "coverage",
        "accuracy",
        "classification",
        "ordering",
        "freshness",
        "aspects",
        "capability",
        "performance",
    }
    assert profile["coverage"]["ok"] == 1
    assert profile["coverage"]["reported"] == 1
    assert profile["coverage"]["expected"] == 1
    assert profile["coverage"]["omitted"] == 0
    assert profile["capability"]["series_members"]["ok"] == 1
    assert "score" not in json.dumps(document["measurements"]).lower()
