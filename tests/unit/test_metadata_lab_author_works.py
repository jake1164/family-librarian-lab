"""WP7: direct author-work cases, offline.

The suite starts every case from an author name, so it never depends on the
book-to-series traversal. These tests prove the cases load, that the fact list and the
checks cannot drift apart, that the new ``forbid_titles`` check means what it says, and
that a provider's capability gap, our adapter gap, a silent answer, a truncated listing
and a wrong merge each land in a different status.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from metadata_lab.cases import CaseValidationError, load_suite_file
from metadata_lab.checks import compute_checks, expected_facts
from metadata_lab.engine import run_suite
from metadata_lab.measurements import FACT_STATUSES, fact_invariant_errors, fact_tables
from metadata_lab.model import AuthorRecord, Operation
from metadata_lab.providers.base import ProviderAdapter, Unsupported

from test_metadata_lab_engine import FakeHttp  # noqa: E402  (shared fake client)

SUITE_PATH = Path(__file__).resolve().parents[2] / "metadata_lab" / "suites" / "author-works.yaml"
AS_OF = "2026-11-16"
EXPECTED_IDS = [
    "aw01-rebecca-yarros",
    "aw02-james-s-a-corey",
    "aw03-jd-robb",
    "aw04-frank-herbert",
    "aw05-stephen-king",
]


@pytest.fixture(scope="module")
def suite():
    return load_suite_file(SUITE_PATH)


def test_five_cases_each_start_from_an_author_name_alone(suite):
    assert [case.id for case in suite.cases] == EXPECTED_IDS
    for case in suite.cases:
        assert set(case.input) == {"author"}, f"{case.id}: must not depend on a book or series input"
        assert case.traversal is False
        assert Operation.AUTHOR_WORKS in case.operations
        assert "author_works" in case.expect and case.expect["author_works"]["titles"]


def test_every_expectation_declares_primary_evidence_and_no_provider_derived_source(suite):
    for case in suite.cases:
        for operation, entry in case.expect.items():
            where = f"{case.id}.{operation}"
            assert entry["source_kind"] in {"primary", "catalogue"}, where
            assert entry["evidence"].strip(), where
            assert entry["verified_on"] >= "2026-11-16", where
            assert entry["confidence"] in {"high", "medium"}, where
        assert case.expect["author_works"].get("exhaustive") is False, f"{case.id}: verified subset only"


def test_fact_list_matches_what_a_perfect_provider_is_checked_on(suite):
    for case in suite.cases:
        for operation in case.operations:
            expect = case.expect[operation.value]
            facts = expected_facts(operation, expect, as_of=AS_OF)
            record = AuthorRecord(
                provider_id="perfect",
                name=str(expect["name"]),
                works=[str(title) for title in expect.get("titles") or []],
            )
            checks = compute_checks(operation, expect, [record], truncated=False, as_of=AS_OF)
            assert {f["name"]: f["dimension"] for f in facts} == {
                c["name"]: c["dimension"] for c in checks
            }, f"{case.id}:{operation.value}"
            assert all(c["outcome"] == "ok" for c in checks), f"{case.id}:{operation.value}"


# --- forbid_titles ---------------------------------------------------------------

HERBERT = next(c for c in load_suite_file(SUITE_PATH).cases if c.id == "aw04-frank-herbert")
HERBERT_EXPECT = HERBERT.expect["author_works"]


def _forbid(records, truncated=False):
    checks = compute_checks(
        Operation.AUTHOR_WORKS, HERBERT_EXPECT, records, truncated=truncated, as_of=AS_OF
    )
    return next(c for c in checks if c["name"] == "author.forbid_titles[0]")


def test_a_namesakes_book_merged_into_the_author_is_a_difference():
    works = [*HERBERT_EXPECT["titles"], "Paul of Dune"]
    result = _forbid([AuthorRecord(provider_id="p", name="Frank Herbert", works=works)])

    assert result["outcome"] == "differs_from_reference"
    assert "Paul of Dune" in result["evidence"]


def test_absence_passes_only_when_the_author_has_works_to_be_absent_from():
    clean = _forbid([AuthorRecord(provider_id="p", name="Frank Herbert", works=["Dune"])])
    empty = _forbid([AuthorRecord(provider_id="p", name="Frank Herbert", works=[])])

    assert clean["outcome"] == "ok"
    assert empty["outcome"] == "not_found", "no works returned must not read as a clean pass"


def test_the_namesake_author_is_not_the_resolved_author():
    brian = AuthorRecord(provider_id="p", name="Brian Herbert", works=["Paul of Dune"])
    checks = compute_checks(Operation.AUTHOR_WORKS, HERBERT_EXPECT, [brian], truncated=False, as_of=AS_OF)

    by_name = {c["name"]: c for c in checks}
    assert by_name["author.name"]["outcome"] == "not_found"
    assert "author.forbid_titles[0]" not in by_name, "no matched author, so nothing to forbid"


def test_forbid_titles_does_not_match_the_prefix_title():
    """'Paul of Dune' must not trip on 'Dune' and 'Dune' must not be forbidden by it."""

    result = _forbid([AuthorRecord(provider_id="p", name="Frank Herbert", works=["Dune", "Dune Messiah"])])

    assert result["outcome"] == "ok"


# --- identification ---------------------------------------------------------------


def _case(suite, case_id):
    return next(c for c in suite.cases if c.id == case_id)


def _name_outcome(case, operation, record_name):
    expect = case.expect[operation]
    record = AuthorRecord(provider_id="p", name=record_name, works=list(expect.get("titles") or []))
    checks = compute_checks(Operation(operation), expect, [record], truncated=False, as_of=AS_OF)
    return next(c for c in checks if c["name"] == "author.name")["outcome"]


def test_a_pen_name_must_be_identified_as_the_pen_name(suite):
    corey = _case(suite, "aw02-james-s-a-corey")

    assert _name_outcome(corey, "author_works", "James S.A. Corey") == "ok"
    assert _name_outcome(corey, "author_works", "James S. A. Corey") == "ok"
    for person in ("Daniel Abraham", "Ty Franck"):
        assert _name_outcome(corey, "author_works", person) != "ok", person


def test_a_pseudonym_may_resolve_to_either_byline_but_not_to_a_stranger(suite):
    robb = _case(suite, "aw03-jd-robb")

    for name in ("J. D. Robb", "J.D. Robb", "Nora Roberts"):
        assert _name_outcome(robb, "author_works", name) == "ok", name
        assert _name_outcome(robb, "author_resolve", name) == "ok", name
    assert _name_outcome(robb, "author_works", "Nora Jones") != "ok"


# --- the four reasons a fact has no value, end to end -----------------------------


def _full(suite):
    by_author = {
        case.input["author"]: case.expect["author_works"]["titles"] for case in suite.cases
    }

    class Full(ProviderAdapter):
        id: ClassVar[str] = "full"
        display_name: ClassVar[str] = "Full"

        def author_resolve(self, ctx, q):
            return [AuthorRecord(provider_id="a", name=q.author or "")]

        def author_works(self, ctx, q):
            return [AuthorRecord(provider_id="a", name=q.author or "", works=list(by_author[q.author]))]

    return Full


def _adapters(suite):
    class Theirs(ProviderAdapter):  # the provider has no such operation (cited)
        id: ClassVar[str] = "theirs"
        display_name: ClassVar[str] = "Theirs"
        unsupported: ClassVar[dict[Operation, Unsupported]] = {
            Operation.AUTHOR_RESOLVE: Unsupported("no author resource", "https://example.org/docs"),
            Operation.AUTHOR_WORKS: Unsupported("no author works", "https://example.org/docs"),
        }

    class NotOurs(ProviderAdapter):  # nothing built yet: our gap
        id: ClassVar[str] = "notours"
        display_name: ClassVar[str] = "Not ours"

    class Silent(ProviderAdapter):  # asked, answered with nothing
        id: ClassVar[str] = "silent"
        display_name: ClassVar[str] = "Silent"

        def author_resolve(self, ctx, q):
            return []

        def author_works(self, ctx, q):
            return []

    class Paged(ProviderAdapter):  # right author, first page only
        id: ClassVar[str] = "paged"
        display_name: ClassVar[str] = "Paged"

        def author_resolve(self, ctx, q):
            return [AuthorRecord(provider_id="a", name=q.author or "")]

        def author_works(self, ctx, q):
            ctx.mark_truncated("first page only")
            return [AuthorRecord(provider_id="a", name=q.author or "", works=[])]

    class Merging(ProviderAdapter):  # attributes the namesake's book to Frank Herbert
        id: ClassVar[str] = "merging"
        display_name: ClassVar[str] = "Merging"

        def author_resolve(self, ctx, q):
            return [AuthorRecord(provider_id="a", name=q.author or "")]

        def author_works(self, ctx, q):
            titles = {c.input["author"]: c.expect["author_works"]["titles"] for c in suite.cases}
            return [
                AuthorRecord(
                    provider_id="a", name=q.author or "", works=[*titles[q.author], "Paul of Dune"]
                )
            ]

    return {cls.id: cls for cls in (_full(suite), Theirs, NotOurs, Silent, Paged, Merging)}


@pytest.fixture(scope="module")
def run_document(suite):
    adapters = _adapters(suite)

    def http_factory(provider_id):
        return FakeHttp()

    # Paged needs ctx.mark_truncated; FakeHttp's context supports it.
    document = run_suite(
        suite,
        list(adapters),
        None,
        http_factory=http_factory,
        adapters=adapters,
        resolve_configured=lambda adapter: True,
        as_of=AS_OF,
    )
    return document


def _bucket(document, provider):
    tables = fact_tables(document["cases"], list(document_providers(document)), as_of=AS_OF)
    assert fact_invariant_errors(tables) == []
    return tables["modes"]["independent"]["providers"][provider]["total"], tables


def document_providers(document):
    return [entry["id"] for entry in document["providers"]]


def test_every_provider_is_held_to_the_same_denominator(run_document, suite):
    expected_total = sum(
        len(expected_facts(op, case.expect[op.value], as_of=AS_OF))
        for case in suite.cases
        for op in case.operations
    )
    for provider in document_providers(run_document):
        bucket, tables = _bucket(run_document, provider)
        assert bucket["facts"] == expected_total, provider
        assert sum(bucket[s] for s in FACT_STATUSES) + bucket["blocked"] == expected_total, provider
    assert tables["modes"]["independent"]["reference_facts"]["total"] == expected_total


def test_provider_gap_and_our_gap_stay_apart(run_document):
    theirs, _ = _bucket(run_document, "theirs")
    notours, _ = _bucket(run_document, "notours")

    assert set(theirs["blocked_by"]) == {"unsupported"}
    assert set(notours["blocked_by"]) == {"not_implemented"}
    assert theirs["blocked"] == notours["blocked"] == theirs["facts"]
    assert theirs["returned"] == notours["returned"] == 0


def test_a_silent_answer_is_missing_metadata_not_a_capability_gap(run_document):
    silent, _ = _bucket(run_document, "silent")

    assert silent["blocked"] == 0
    assert silent["omitted"] == silent["facts"]
    assert silent["reachable"] == silent["facts"]


def test_a_truncated_listing_is_incomplete_not_a_clean_miss(run_document):
    paged, _ = _bucket(run_document, "paged")
    silent, _ = _bucket(run_document, "silent")

    assert paged["incomplete"] == 40, "every title it could not list is incomplete"
    assert paged["omitted"] == 1, "only the forbid check, which has nothing to be absent from"
    assert silent["incomplete"] == 0
    assert paged["blocked"] == 0


def test_a_perfect_provider_is_all_correct_and_a_merging_one_is_caught(run_document):
    full, _ = _bucket(run_document, "full")
    merging, _ = _bucket(run_document, "merging")

    assert full["correct"] == full["facts"]
    assert merging["differs"] == 1, "exactly the one forbidden Herbert work"
    assert merging["correct"] == merging["facts"] - 1


def test_an_author_input_is_required(tmp_path):
    text = SUITE_PATH.read_text(encoding="utf-8").replace(
        'author: "Frank Herbert"', 'title: "Dune"', 1
    )
    target = tmp_path / "missing-author.yaml"
    target.write_text(text, encoding="utf-8")

    with pytest.raises(CaseValidationError, match="author"):
        load_suite_file(target)
