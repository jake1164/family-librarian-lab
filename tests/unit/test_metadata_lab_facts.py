"""Fixed-denominator measurement model (metric version 2).

Offline only. The central rule: every provider is measured against the same N
reference facts, each fact lands in exactly one status, and facts a provider could
not be asked are *blocked* (never dropped), so a provider that cannot run an
operation does not quietly get a smaller denominator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metadata_lab.cases import load_suite_file
from metadata_lab.checks import compute_checks, expected_facts
from metadata_lab.export import ai_export
from metadata_lab.measurements import (
    FACT_STATUSES,
    blocked_class,
    fact_display,
    fact_invariant_errors,
    fact_tables,
    measurements,
)
from metadata_lab.model import (
    AuthorRecord,
    BookRecord,
    Operation,
    SeriesMember,
    SeriesRecord,
    SeriesRef,
)
from metadata_lab.report import render_report

SUITES_DIR = Path(__file__).resolve().parents[2] / "metadata_lab" / "suites"
QUALITY_FILES = sorted((SUITES_DIR / "quality").glob("p*.yaml"))
AS_OF = "2026-11-14"


# --- a provider that answers every expectation perfectly ------------------------


def _perfect_records(operation: Operation, expect: dict):
    if operation in (Operation.BOOK_BY_TITLE, Operation.BOOK_BY_ISBN):
        book = BookRecord(
            provider_id="perfect",
            title=str(expect.get("title") or "A Title"),
            authors=[str(a) for a in expect.get("authors") or []],
            isbn13=[str(expect["isbn13"])] if "isbn13" in expect else [],
            published=expect.get("published"),
            first_published=expect.get("first_published"),
            work_type=str(expect.get("work_type", "unknown")),
            format=str(expect.get("format", "unknown")),
            publisher="Publisher",
            pages=100,
            language="en",
            cover=True,
            description="text",
        )
        return [book]
    if operation in (Operation.SERIES_FOR_BOOK, Operation.SERIES_MEMBERS):
        members = [
            SeriesMember(
                title=str(m.get("title", "")),
                position=str(m["position"]) if m.get("position") is not None else (
                    str((m.get("accepted_positions") or ["1"])[0])
                ),
                authors=[str(a) for a in m.get("authors") or []],
                work_type=str(m.get("work_type", "novel")),
                published=m.get("published"),
            )
            for m in expect.get("members") or []
        ]
        return [SeriesRecord(provider_id="perfect", name=str(expect.get("series", "S")), members=members)]
    return [
        AuthorRecord(
            provider_id="perfect",
            name=str(expect.get("name") or "An Author"),
            works=[str(t) for t in expect.get("titles") or []],
            series=[SeriesRef(name=str(s)) for s in expect.get("series") or []],
        )
    ]


def _case_params():
    params = []
    for path in QUALITY_FILES:
        for case in load_suite_file(path).cases:
            for operation in case.operations:
                params.append(pytest.param(case, operation, id=f"{case.id}:{operation.value}"))
    return params


@pytest.mark.parametrize(("case", "operation"), _case_params())
@pytest.mark.parametrize("as_of", [AS_OF, "2999-01-01"])
def test_fact_list_matches_what_a_perfect_provider_is_checked_on(case, operation, as_of):
    """The enumerator and the checks can never drift apart (names and dimensions)."""

    expect = case.expect.get(operation.value) or {}
    facts = expected_facts(operation, expect, as_of=as_of)
    checks = compute_checks(
        operation,
        expect,
        _perfect_records(operation, expect),
        truncated=False,
        context_terms=(),
        as_of=as_of,
    )

    assert {f["name"]: f["dimension"] for f in facts} == {
        c["name"]: c["dimension"] for c in checks
    }
    assert len({f["name"] for f in facts}) == len(facts), "fact names must be unique"


def test_every_shipped_fixture_contributes_facts():
    total = 0
    for path in QUALITY_FILES:
        for case in load_suite_file(path).cases:
            total += sum(
                len(expected_facts(op, case.expect.get(op.value) or {}, as_of=AS_OF))
                for op in case.operations
            )
    assert total > 100


# --- synthetic runs --------------------------------------------------------------


def _row(provider, operation, outcome, checks=(), *, mode="independent", reason=None, error=None):
    row = {
        "provider": provider,
        "operation": operation,
        "mode": mode,
        "outcome": outcome,
        "checks": list(checks),
    }
    if reason:
        row["reason"] = reason
    if error:
        row["error"] = error
    return row


def _chk(name, outcome, dimension):
    return {"name": name, "outcome": outcome, "dimension": dimension}


EXPECT_SERIES = {
    "series": "Saga",
    "members": [
        {"title": "One", "position": "1"},
        {"title": "Two", "position": "2"},
    ],
}
# series.name + 2 x (present, position) = 5 facts.


def _cases():
    return [
        {
            "id": "c1",
            "operations": ["series_members", "author_series"],
            "expect": {
                "series_members": EXPECT_SERIES,
                "author_series": {"name": "Ann Author", "series": ["Saga", "Other"]},
            },
            "results": [
                # full answer, one position wrong
                _row(
                    "full",
                    "series_members",
                    "partial",
                    [
                        _chk("series.name", "ok", "coverage"),
                        _chk("series.member[0].present", "ok", "coverage"),
                        _chk("series.member[0].position", "ok", "ordering"),
                        _chk("series.member[1].present", "ok", "coverage"),
                        _chk("series.member[1].position", "differs_from_reference", "ordering"),
                    ],
                ),
                # the series itself is not found: no member checks are emitted at all
                _row("lost", "series_members", "not_found", [_chk("series.name", "not_found", "coverage")]),
                _row("notours", "series_members", "not_implemented"),
                _row("theirs", "series_members", "unsupported"),
                _row("down", "series_members", "not_run", reason="cooling_down"),
                _row("broke", "series_members", "error", error={"kind": "rate_limited", "message": "x"}),
                _row("full", "author_series", "ok", [
                    _chk("author.name", "ok", "coverage"),
                    _chk("author.series[0]", "ok", "coverage"),
                    _chk("author.series[1]", "ok", "coverage"),
                ]),
                _row("lost", "author_series", "not_found", [_chk("author.name", "not_found", "coverage")]),
                _row("notours", "author_series", "not_implemented"),
                _row("theirs", "author_series", "unsupported"),
                _row("down", "author_series", "not_run", reason="cooling_down"),
                _row("broke", "author_series", "error", error={"kind": "timeout", "message": "x"}),
            ],
        }
    ]


PROVIDERS = ["full", "lost", "notours", "theirs", "down", "broke"]


def test_every_provider_has_the_same_n_and_the_statuses_add_up():
    tables = fact_tables(_cases(), PROVIDERS, as_of=AS_OF)
    block = tables["modes"]["independent"]

    assert block["reference_facts"]["total"] == 5 + 3  # series_members 5, author_series 3
    for provider in PROVIDERS:
        bucket = block["providers"][provider]["total"]
        assert bucket["facts"] == 8
        assert sum(bucket[s] for s in FACT_STATUSES) + bucket["blocked"] == 8
    assert fact_invariant_errors(tables) == []


def test_a_provider_that_cannot_run_an_operation_keeps_its_denominator():
    """The P07 problem: not_implemented must not shrink N (it used to read 1/1)."""

    block = fact_tables(_cases(), PROVIDERS, as_of=AS_OF)["modes"]["independent"]["providers"]

    assert block["notours"]["total"]["blocked_by"] == {"not_implemented": 8}
    assert block["notours"]["total"]["reachable"] == 0
    assert block["theirs"]["total"]["blocked_by"] == {"unsupported": 8}
    assert block["down"]["total"]["blocked_by"] == {"cooling_down": 8}
    assert block["broke"]["total"]["blocked_by"] == {"error:rate_limited": 5, "error:timeout": 3}
    assert blocked_class("unsupported") == "provider"
    assert blocked_class("not_implemented") == "tool"
    assert blocked_class("cooling_down") == "run"


def test_facts_skipped_because_the_series_was_not_found_count_as_omitted():
    """Member checks are never emitted when the series name misses; they are omitted, not absent."""

    bucket = fact_tables(_cases(), PROVIDERS, as_of=AS_OF)["modes"]["independent"]["providers"]["lost"]["total"]

    assert bucket["omitted"] == 8  # 5 series facts + 3 author facts
    assert bucket["blocked"] == 0
    assert bucket["returned"] == 0


def test_returned_accuracy_excludes_omission_and_blocked_facts():
    bucket = fact_tables(_cases(), PROVIDERS, as_of=AS_OF)["modes"]["independent"]["providers"]["full"]["total"]

    assert (bucket["correct"], bucket["differs"], bucket["returned"]) == (7, 1, 8)
    assert bucket["reachable"] == 8


def test_modes_are_never_mixed():
    cases = _cases()
    cases[0]["results"].append(
        _row("full", "series_members", "ok", [_chk("series.name", "ok", "coverage")], mode="traversal")
    )
    tables = fact_tables(cases, PROVIDERS, as_of=AS_OF)

    assert set(tables["modes"]) == {"independent", "traversal"}
    assert tables["modes"]["independent"]["reference_facts"]["total"] == 8
    # Only the series_members pair has a traversal row, and every provider still gets all 5 of its facts.
    assert tables["modes"]["traversal"]["reference_facts"]["total"] == 5
    assert tables["modes"]["traversal"]["providers"]["lost"]["total"]["blocked_by"] == {"no_row": 5}
    assert fact_invariant_errors(tables) == []


def test_invariant_check_catches_a_tampered_table():
    tables = fact_tables(_cases(), PROVIDERS, as_of=AS_OF)
    tables["modes"]["independent"]["providers"]["full"]["total"]["correct"] += 1

    assert any("full/total" in error for error in fact_invariant_errors(tables))


def test_an_unlisted_check_is_reported_not_silently_counted():
    cases = _cases()
    cases[0]["results"][0]["checks"].append(_chk("series.surprise", "ok", "coverage"))
    tables = fact_tables(cases, PROVIDERS, as_of=AS_OF)

    assert any("not in the fact list" in error for error in fact_invariant_errors(tables))


def test_expired_reference_is_one_needs_review_fact():
    expect = {
        "series": "Saga",
        "members": [{"title": "One", "position": "1", "valid_until": "2020-01-01"}],
    }
    facts = expected_facts(Operation.SERIES_MEMBERS, expect, as_of=AS_OF)

    assert [f["name"] for f in facts] == ["series.name", "series.member[0]"]
    assert facts[1]["dimension"] == "freshness"


def test_display_prints_no_percentage_for_a_handful_of_returned_values():
    tables = fact_tables(_cases(), PROVIDERS, as_of=AS_OF)["modes"]["independent"]["providers"]
    few = fact_display(tables["lost"]["total"])
    many_bucket = dict(tables["full"]["total"], returned=10, correct=9)

    assert "n<5" in fact_display(dict(tables["full"]["total"], returned=2, correct=2))
    assert "returned agree" in few
    assert "9/10 of returned agree" in fact_display(many_bucket)
    assert "blocked not_implemented=8" in fact_display(tables["notours"]["total"])


def test_measurements_block_carries_the_facts_and_the_legacy_tallies():
    block = measurements(_cases(), PROVIDERS, as_of=AS_OF)

    assert block["facts"]["metric_version"] == 2
    assert "by_provider" in block and "by_case" in block


def test_ai_export_and_report_show_the_same_n_for_every_provider():
    document = {
        "run": {"id": "r", "mode": "live", "status": "complete", "as_of": AS_OF, "harness": {"status": "validated"}},
        "suite": {"name": "s", "version": 1},
        "providers": [{"id": p, "display_name": p, "configured": True, "verified_live": True} for p in PROVIDERS],
        "cases": _cases(),
        "matrix": {},
        "matrix_checks": {},
        "measurements": measurements(_cases(), PROVIDERS, as_of=AS_OF),
    }

    ai = ai_export(document)
    summary = ai["fact_summary"]["independent"]
    html = render_report(document)

    assert summary["reference_facts"] == 8
    assert all(entry["all"].split(" returned")[0].endswith("/8") for entry in summary["providers"].values())
    assert "same denominator for every provider" in html
    assert "not_implemented=8 (tool)" in html
    assert "unsupported=8 (provider)" in html


def test_one_non_conflicting_observation_scores_the_field_and_keeps_identity_ambiguous():
    """Decision: field presence uses the unique non-conflicting observation; series identity stays ambiguous."""

    name = _chk("series.name", "ok", "coverage")
    held = dict(
        _chk("series.member[0].present", "ambiguous", "coverage"),
        merge={"records": 5, "holding": 1, "conflicting": False, "resolved": "ok"},
    )
    fuzzy_only = dict(
        _chk("series.member[1].present", "ambiguous", "coverage"),
        merge={"records": 2, "holding": 1, "conflicting": False, "resolved": None},
    )
    cases = [
        {
            "id": "c",
            "operations": ["series_members"],
            "expect": {
                "series_members": {
                    "series": "Saga",
                    "members": [{"title": "One"}, {"title": "Two"}, {"title": "Three"}],
                }
            },
            "results": [
                _row(
                    "p",
                    "series_members",
                    "partial",
                    [
                        name,
                        held,
                        fuzzy_only,
                        dict(
                            _chk("series.member[2].present", "ambiguous", "coverage"),
                            merge={"records": 2, "holding": 2, "conflicting": True, "resolved": None},
                        ),
                    ],
                )
            ],
        }
    ]
    bucket = fact_tables(cases, ["p"], as_of=AS_OF)["modes"]["independent"]["providers"]["p"]["total"]

    assert bucket["correct"] == 2  # the series name and the one held member
    assert bucket["identity_ambiguous"] == 1
    assert bucket["ambiguous"] == 2  # a fuzzy-only observation and a real conflict stay ambiguous
    assert bucket["ambiguous_no_conflict"] == 1
    text = fact_display(bucket)
    assert "series identity ambiguous" in text
    assert "no conflicting record" in text
    assert fact_invariant_errors(fact_tables(cases, ["p"], as_of=AS_OF)) == []


def test_merge_records_whether_a_real_conflict_existed_and_what_was_observed():
    from metadata_lab.checks import _merge

    ok = ("a", _chk("x", "ok", "coverage") | {"expected": "t", "observed": "t"})
    missing = ("b", _chk("x", "not_found", "coverage") | {"expected": "t", "observed": None})
    differs = ("c", _chk("x", "differs_from_reference", "coverage") | {"expected": "t", "observed": "u"})

    lonely = _merge("x", [ok, missing, missing], "coverage")
    wrong_alone = _merge("x", [differs, missing], "coverage")
    clash = _merge("x", [ok, differs], "coverage")

    assert lonely["outcome"] == wrong_alone["outcome"] == clash["outcome"] == "ambiguous"
    assert lonely["merge"] == {"records": 3, "holding": 1, "conflicting": False, "resolved": "ok"}
    assert wrong_alone["merge"]["resolved"] == "differs_from_reference"
    assert clash["merge"] == {"records": 2, "holding": 2, "conflicting": True, "resolved": None}
