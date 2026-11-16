"""The run-wide request cap: real HTTP requests per provider, retries included.

Offline: a real ``HttpClient`` over an injected transport, driven through ``run_suite``.
The cap must hold even when every call retries, later rows must be skipped under their
own labelled reason, and a capped provider's missing facts must not read as the
provider's silence.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from metadata_lab.cases import Case, Reference, Suite
from metadata_lab.engine import REQUEST_BUDGET_REASON, run_suite
from metadata_lab.http import HttpClient, RequestBudget, TransportResponse
from metadata_lab.measurements import fact_tables
from metadata_lab.model import AuthorRecord, Operation
from metadata_lab.providers.base import ProviderAdapter

URL = "https://api.example.org/v1/authors"


class CountingTransport:
    """Answers with a script and counts every request that reaches the wire."""

    def __init__(self, script):
        self.script = script
        self.sent = 0

    def send(self, method, url, *, params, headers, body, timeout):
        self.sent += 1
        item = self.script(self.sent)
        if isinstance(item, BaseException):
            raise item
        return item


def ok(_n):
    return TransportResponse(200, {}, '{"works": ["Dune"]}')


def always_503(_n):
    return TransportResponse(503, {}, "{}")


class Pager(ProviderAdapter):
    """One author_works call that asks for three pages."""

    id: ClassVar[str] = "pager"
    display_name: ClassVar[str] = "Pager"

    def author_works(self, ctx, q):
        works: list[str] = []
        for page in range(3):
            payload = ctx.get_json(URL, params={"q": q.author, "page": page})
            works.extend(payload.get("works") or [])
        return [AuthorRecord(provider_id="a", name=q.author or "", works=works)]


def suite_of(count: int) -> Suite:
    cases = tuple(
        Case(
            id=f"c{index}",
            description="x",
            reference=Reference(source="test", verified_on="2026-11-16"),
            input={"author": f"Author {index}"},
            operations=(Operation.AUTHOR_WORKS,),
            expect={"author_works": {"name": f"Author {index}", "titles": ["Dune"]}},
            verified=True,
        )
        for index in range(count)
    )
    return Suite(name="s", version=1, cases=cases, source="memory", sha256="0" * 64)


def run(script, *, cap, cases=6, transport=None):
    transport = transport or CountingTransport(script)

    def factory(_pid):
        return HttpClient(
            provider_id="pager", transport=transport, sleep=lambda _s: None, min_interval_s=0.0
        )

    document = run_suite(
        suite_of(cases),
        None,
        None,
        http_factory=factory,
        adapters={"pager": Pager},
        resolve_configured=lambda adapter: True,
        as_of="2026-11-16",
        max_requests_per_provider=cap,
    )
    return document, transport


def rows(document):
    return [row for case in document["cases"] for row in case["results"]]


@pytest.mark.parametrize("bad", [0, -3, True, 2.5])
def test_a_bad_cap_is_refused_before_anything_runs(bad):
    transport = CountingTransport(ok)

    with pytest.raises(ValueError):
        run(ok, cap=bad, transport=transport)
    assert transport.sent == 0


def test_without_a_cap_nothing_changes():
    document, transport = run(ok, cap=None, cases=2)

    assert transport.sent == 6
    assert "request_budget" not in document["run"]


def test_the_cap_holds_and_later_rows_are_skipped_under_their_own_reason():
    document, transport = run(ok, cap=7)

    assert transport.sent <= 7
    skipped = [row for row in rows(document) if row.get("reason") == REQUEST_BUDGET_REASON]
    assert skipped, "rows after the cap must be skipped, not run"
    assert all(row["outcome"] == "not_run" and row["stats"]["calls"] == 0 for row in skipped)
    assert document["run"]["request_budget"]["limit"] == 7
    assert document["run"]["request_budget"]["used"]["pager"] == transport.sent


def test_the_cap_holds_even_when_every_call_retries():
    """One logical call can become three requests; the worst case is reserved up front."""

    document, transport = run(always_503, cap=10)

    assert transport.sent <= 10
    assert document["run"]["request_budget"]["used"]["pager"] == transport.sent


def test_an_operation_that_hits_the_cap_midway_is_truncated_not_not_found():
    document, _ = run(ok, cap=6, cases=3)

    first, second, third = rows(document)
    assert first["truncated"] is False and first["stats"]["calls"] == 3
    assert second["truncated"] is True and second["stats"]["calls"] == 1
    assert any("request cap" in note for note in second["notes"])
    assert {check["outcome"] for check in second["checks"]} == {"incomplete"}
    assert third["reason"] == REQUEST_BUDGET_REASON


def test_capped_rows_count_as_blocked_run_conditions_not_provider_silence():
    document, _ = run(ok, cap=7)

    block = fact_tables(document["cases"], ["pager"], as_of="2026-11-16")["modes"]["independent"]
    total = block["providers"]["pager"]["total"]

    assert total["blocked_by"].get(REQUEST_BUDGET_REASON, 0) > 0
    assert total["facts"] == block["reference_facts"]["total"], "the denominator does not shrink"


def test_a_cap_with_traversal_is_refused():
    suite = suite_of(1)

    with pytest.raises(ValueError, match="traversal"):
        run_suite(
            suite,
            None,
            None,
            http_factory=lambda _pid: HttpClient(
                provider_id="pager", transport=CountingTransport(ok), sleep=lambda _s: None
            ),
            adapters={"pager": Pager},
            resolve_configured=lambda adapter: True,
            traversal=True,
            max_requests_per_provider=20,
        )


def test_a_cache_answer_is_not_an_http_request():
    budget = RequestBudget(10)
    budget.used = 1

    from metadata_lab.http import OpContext

    transport = CountingTransport(ok)
    sender = HttpClient(provider_id="p", transport=transport, sleep=lambda _s: None, min_interval_s=0.0)
    ctx = OpContext(sender, request_budget=budget)
    ctx._settle_request_budget(0, True)

    assert budget.used == 0
