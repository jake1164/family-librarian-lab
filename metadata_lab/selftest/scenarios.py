"""H01-H24: does the harness record exactly what happened?

Every scenario drives the real engine, HTTP helper, exports and report against a
loopback scripted server. A scenario returns ``Evidence(True, detail)`` or raises
``AssertionError``; the runner turns the latter into ``FAIL``.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .. import registry
from ..cases import Case, CaseValidationError, Reference, load_suite_file
from ..engine import run_oneoff, run_suite
from ..export import ai_export, full_export
from ..http import HttpClient, Transport, TransportResponse
from ..model import ErrorKind, Operation, Outcome, ProviderError
from ..providers.base import ProviderAdapter, Unsupported
from ..replay import ReplayHttp
from ..report import render_report
from ..semantic import semantic_diff, semantic_digest, semantic_view
from .adapters import failing_adapter, make_adapter
from .runner import Evidence, Harness, scenario
from .server import ScriptedResponse, scripted

TITLE = "Fourth Wing"
SECRET = "SELFTEST-SECRET-0001"

BOOKS = {
    "books": [
        {
            "id": "b1",
            "title": TITLE,
            "authors": ["Rebecca Yarros"],
            "isbn13": ["9781649374042"],
            "publisher": "Red Tower Books",
            "published": "2023-05-02",
            "pages": 517,
            "language": "eng",
            "cover": True,
            "work_type": "novel",
            "series": [{"name": "The Empyrean", "position": "1", "id": "s1"}],
        }
    ]
}

EMPTY_BOOKS = {"books": []}

SERIES_OK = {
    "series": [
        {
            "id": "s1",
            "name": "The Empyrean",
            "members": [
                {
                    "id": "w1",
                    "title": "Fourth Wing",
                    "position": "1",
                    "work_type": "novel",
                    "authors": ["Rebecca Yarros"],
                    "published": "2023-05-02",
                },
                {
                    "id": "w2",
                    "title": "Threshing Day",
                    "position": "3.5",
                    "work_type": "collection",
                    "authors": ["Rebecca Yarros"],
                    "published": "2026-09-29",
                },
            ],
        }
    ],
    "total": 2,
    "done": True,
}

AUTHORS = {
    "authors": [
        {
            "id": "a1",
            "name": "Rebecca Yarros",
            "aliases": ["Yarros, Rebecca"],
            "works": ["Fourth Wing", "Iron Flame"],
            "series": [{"name": "The Empyrean", "id": "s1"}],
        }
    ]
}

MEMBER_EXPECT = {
    "series_members": {
        "series": "The Empyrean",
        "members": [
            {"title": "Fourth Wing", "position": "1", "work_type": "novel", "published": "2023-05-02"},
            {
                "title": "Threshing Day",
                "position": "3.5",
                "work_type": "collection",
                "published": "2026-09-29",
            },
        ],
    }
}


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _row(document: Mapping[str, Any], provider: str = "alpha", operation: str = "series_members"):
    for case in document.get("cases", []):
        for row in case.get("results", []):
            if row["provider"] == provider and row["operation"] == operation:
                return row
    raise AssertionError(f"no {provider}/{operation} row in the document")


def _checks(row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in row.get("checks", [])}


def _seed(harness: Harness, *, books: Any = None, series: Any = None, authors: Any = None) -> None:
    harness.server.queue("/books", scripted(200, BOOKS if books is None else books))
    harness.server.queue("/series", scripted(200, SERIES_OK if series is None else series))
    harness.server.queue("/authors", scripted(200, AUTHORS if authors is None else authors))


def _adapter(harness: Harness, provider_id: str = "alpha", **kwargs: Any):
    return make_adapter(harness.server.base_url, provider_id, **kwargs)


# --- H01-H04: entry points and isolation -------------------------------------------


@scenario("H01", "easy", "one-off query writes result.json, ai.json and report.html")
def h01(harness: Harness) -> Evidence:
    _seed(harness)
    document = run_oneoff(
        "book",
        {"title": TITLE},
        ["alpha"],
        http_factory=harness.http_factory({"alpha": _adapter(harness)}),
        adapters={"alpha": _adapter(harness)},
    )
    row = _row(document, operation="book_by_title")
    _require(row["outcome"] == "ok", f"expected ok, got {row['outcome']}")
    paths = harness.write_outputs(document, "h01")
    json.loads(paths["result"].read_text(encoding="utf-8"))
    json.loads(paths["ai"].read_text(encoding="utf-8"))
    _require("<html" in paths["report"].read_text(encoding="utf-8"), "report is not HTML")
    return Evidence(True, "one-off recorded and all three outputs parse")


@scenario("H02", "easy", "five providers produce one independent row each")
def h02(harness: Harness) -> Evidence:
    _seed(harness)
    adapters = {name: _adapter(harness, name) for name in ("alpha", "beta", "gamma", "delta", "epsilon")}
    document = harness.run(adapters, operations=(Operation.SERIES_MEMBERS,))
    rows = document["cases"][0]["results"]
    _require(len(rows) == 5, f"expected 5 rows, got {len(rows)}")
    _require(len({row["provider"] for row in rows}) == 5, "providers were not kept separate")
    for row in rows:
        _require(row["mode"] == "independent", "row mode should be independent")
        _require(isinstance(row["elapsed_ms"], int), "per-row timing missing")
        _require(row["outcome"] == "ok", f"{row['provider']} not ok: {row['outcome']}")
    return Evidence(True, "5 independent rows, no shared state, timings recorded")


@scenario("H03", "easy", "--exclude leaves the provider not_run with reason excluded")
def h03(harness: Harness) -> Evidence:
    _seed(harness)
    adapters = {"alpha": _adapter(harness, "alpha"), "beta": _adapter(harness, "beta")}
    document = harness.run(adapters, exclude=["beta"])
    rows = {row["provider"]: row for row in document["cases"][0]["results"]}
    _require(rows["beta"]["outcome"] == "not_run", "excluded provider should be not_run")
    _require(rows["beta"]["reason"] == "excluded", "excluded provider needs reason=excluded")
    _require(rows["alpha"]["outcome"] == "ok", "the other provider should be unaffected")
    _require(harness.server.calls("/series") != [], "the active provider should have been called")
    return Evidence(True, "beta not_run/excluded; alpha unaffected")


@scenario("H04", "medium", "saved case and one-off agree through the shared engine")
def h04(harness: Harness) -> Evidence:
    _seed(harness)
    adapters = {"alpha": _adapter(harness)}
    saved = harness.run(adapters, operations=(Operation.SERIES_MEMBERS,), expect={})
    oneoff = run_oneoff(
        "series",
        {"series": "The Empyrean"},
        ["alpha"],
        http_factory=harness.http_factory(adapters),
        adapters=adapters,
    )
    saved_row = saved["cases"][0]["results"][0]
    oneoff_row = oneoff["cases"][0]["results"][0]
    _require(saved_row["outcome"] == oneoff_row["outcome"], "outcomes differ")
    _require(saved_row["records"] == oneoff_row["records"], "normalised records differ")
    _require(saved_row["checks"] == oneoff_row["checks"], "checks differ")
    return Evidence(True, f"identical outcome ({saved_row['outcome']}) and records")


# --- H05-H09: failures, credentials, capability ------------------------------------


@scenario("H05", "medium", "batch of ten cases isolates three failures")
def h05(harness: Harness) -> Evidence:
    cases = []
    kinds = ["ok"] * 7 + ["http", "malformed", "exception"]
    for index, kind in enumerate(kinds):
        cases.append(
            Case(
                id=f"c{index}",
                description=kind,
                reference=Reference(source="selftest"),
                input={"series": "The Empyrean", "author": "Rebecca Yarros"},
                operations=(Operation.SERIES_MEMBERS,),
                expect={},
            )
        )

    _seed(harness)
    adapters = {"alpha": _adapter(harness)}
    healthy = harness.run(adapters, cases=cases[:7])
    outcomes = [row["outcome"] for case in healthy["cases"] for row in case["results"]]
    _require(len(healthy["cases"]) == 7, "seven healthy cases should run")
    _require(all(outcome == "ok" for outcome in outcomes), f"healthy cases failed: {outcomes}")

    failing_http = failing_adapter("boom", ProviderError(ErrorKind.HTTP, "HTTP 500"))
    failing_json = failing_adapter("badjson", ValueError("adapter raised"))
    failures = harness.run({"boom": failing_http, "badjson": failing_json}, cases=cases[7:])
    failed_outcomes = [row["outcome"] for case in failures["cases"] for row in case["results"]]
    _require(all(outcome == "error" for outcome in failed_outcomes), f"expected errors, got {failed_outcomes}")
    _require(len(failed_outcomes) == 6, "every failing case must still be reported twice (two providers)")
    return Evidence(True, "10 cases reported; failures isolated to their own rows")


@scenario("H06", "medium", "missing credential vs invalid credential")
def h06(harness: Harness) -> Evidence:
    _seed(harness)
    missing_env = "MDLAB_SELFTEST_ABSENT_TOKEN"
    import os

    os.environ.pop(missing_env, None)
    keyed = _adapter(harness, "keyed", credential_env=missing_env)
    document = harness.run({"keyed": keyed})
    _require(
        _row(document, provider="keyed")["outcome"] == "not_configured",
        "missing credential should be not_configured",
    )
    _require(harness.server.calls("/series") == [], "no call should be made without a credential")

    harness.server.reset()
    harness.server.queue("/series", scripted(401, {"error": "invalid token"}))
    os.environ["MDLAB_SELFTEST_TOKEN"] = SECRET
    try:
        keyed_invalid = _adapter(harness, "keyed2", credential_env="MDLAB_SELFTEST_TOKEN")
        invalid = harness.run({"keyed2": keyed_invalid})
        row = _row(invalid, provider="keyed2")
        _require(row["outcome"] == "error", f"invalid credential should error, got {row['outcome']}")
        _require(row["error"]["kind"] == "auth", "invalid credential must be error/auth")
        _require(SECRET not in json.dumps(invalid), "the planted secret leaked into the result")
    finally:
        os.environ.pop("MDLAB_SELFTEST_TOKEN", None)
    return Evidence(True, "not_configured vs error/auth, secret absent")


@scenario("H07", "easy", "an empty result is not_found, never an error")
def h07(harness: Harness) -> Evidence:
    harness.server.queue("/books", scripted(200, EMPTY_BOOKS))
    document = harness.run({"alpha": _adapter(harness)}, operations=(Operation.BOOK_BY_TITLE,))
    row = _row(document, operation="book_by_title")
    _require(row["outcome"] == "not_found", f"expected not_found, got {row['outcome']}")
    _require(row["error"] is None, "an empty result must not be an error")
    return Evidence(True, "not_found with no error")


@scenario("H08", "easy", "unsupported (cited) vs not_implemented, and no request for either")
def h08(harness: Harness) -> Evidence:
    _seed(harness)
    declared = _adapter(
        harness,
        "declared",
        unsupported={
            Operation.SERIES_MEMBERS: Unsupported(
                reason="documented: no series endpoint", source_url="https://example.org/api"
            )
        },
    )

    class DeclaredOnly(declared):
        # Restore the base method so the operation is "not written yet".
        author_series = ProviderAdapter.author_series

    document = harness.run({"declared": DeclaredOnly}, operations=(Operation.SERIES_MEMBERS, Operation.AUTHOR_SERIES))
    rows = {row["operation"]: row for row in document["cases"][0]["results"]}
    _require(rows["series_members"]["outcome"] == "unsupported", "declared op should be unsupported")
    _require("no series endpoint" in rows["series_members"]["notes"][0], "the reason must be shown")
    _require(rows["author_series"]["outcome"] == "not_implemented", "unwritten op should be not_implemented")
    _require(harness.server.calls("/series") == [], "no request may be sent for either case")
    return Evidence(True, "unsupported and not_implemented kept distinct, zero requests")


@scenario("H09", "hard", "429/timeout/500 classified, retry bound honoured, no real waiting")
def h09(harness: Harness) -> Evidence:
    _seed(harness)
    adapters = {"alpha": _adapter(harness)}
    harness.server.reset()
    harness.server.queue("/series", *[scripted(429, {}, headers={"Retry-After": "1"}) for _ in range(3)])
    document = harness.run(adapters)
    row = _row(document)
    _require(row["error"]["kind"] == "rate_limited", f"expected rate_limited, got {row['error']}")
    _require(len(harness.server.calls("/series")) == 3, "retry bound should be 3 attempts")
    _require(row["stats"]["retries"] == 2 and row["stats"]["throttled"] == 3, "stats should count retries/429s")

    class TimeoutTransport:
        def send(self, method, url, *, params, headers, body, timeout):
            raise TimeoutError("scripted timeout")

    harness.transports["beta"] = TimeoutTransport()
    harness.server.reset()
    harness.server.queue("/series", scripted(200, SERIES_OK))
    timeout_doc = harness.run({"beta": _adapter(harness, "beta")}, providers=["beta"])
    _require(_row(timeout_doc, provider="beta")["error"]["kind"] == "timeout", "expected timeout")

    harness.server.reset()
    harness.server.queue("/series", *[scripted(500, {}) for _ in range(3)])
    http_doc = harness.run({"alpha": _adapter(harness)})
    _require(_row(http_doc)["error"]["kind"] == "http", "expected http error")
    _require(harness.clock.slept, "the retry backoff should have been counted on the fake clock")
    _require(all(delay <= 20 for delay in harness.clock.slept), "no sleep above the Retry-After cap")

    harness.transports.clear()
    harness.server.reset()
    harness.server.queue("/series", *[scripted(500, {}) for _ in range(3)])
    harness.server.queue("/series", scripted(200, SERIES_OK))
    isolated = harness.run({"alpha": _adapter(harness, "alpha"), "beta": _adapter(harness, "beta")})
    outcomes = {row["provider"]: row["outcome"] for row in isolated["cases"][0]["results"]}
    _require("error" in outcomes.values(), "one provider should have failed")
    _require("ok" in outcomes.values(), "the other provider should still have run")
    return Evidence(True, "rate_limited/timeout/http classified; 3 attempts each; others continue")


# --- H10-H13: pagination, agreement, replay, canaries -----------------------------


@scenario("H10", "hard", "three pages, cap, duplicates and the pagination block")
def h10(harness: Harness) -> Evidence:
    page1 = {
        "series": [
            {
                "id": "s1",
                "name": "The Empyrean",
                "members": [{"id": "w1", "title": "Fourth Wing", "position": "1"}],
            }
        ],
        "total": 4,
        "next": True,
    }
    page2 = {
        "series": [
            {
                "id": "s1",
                "name": "The Empyrean",
                "members": [{"id": "w1", "title": "Fourth Wing", "position": "1"}],
            }
        ],
        "total": 4,
        "next": True,
    }
    page3 = {
        "series": [
            {
                "id": "s1",
                "name": "The Empyrean",
                "members": [{"id": "w9", "title": "Onyx Storm", "position": "3"}],
            }
        ],
        "total": 4,
        "done": True,
    }
    harness.server.queue("/series", scripted(200, page1), scripted(200, page2), scripted(200, page3))
    document = harness.run(
        {"alpha": _adapter(harness)},
        options={"max_pages": 2},
        expect={"series_members": {"series": "The Empyrean", "members": [{"title": "Onyx Storm", "position": "3"}]}},
    )
    row = _row(document)
    _require(row["truncated"] is True, "the page cap should mark the row truncated")
    _require(row["outcome"] == "incomplete", f"expected incomplete, got {row['outcome']}")
    _require(row["pagination"]["pages_fetched"] == 2, "pagination should show 2 pages fetched")
    _require(row["duplicates"], "the repeated member should be reported as a duplicate")
    _require(row["records"], "duplicate records must still be retained")
    return Evidence(True, "cap -> truncated+incomplete, duplicates reported and retained")


@scenario("H11", "hard", "two conflicting series are both kept and reported ambiguous")
def h11(harness: Harness) -> Evidence:
    conflicting = {
        "series": [
            {"id": "s1", "name": "The Empyrean", "members": [{"id": "w1", "title": "Fourth Wing", "position": "1"}]},
            {"id": "s2", "name": "The Empyrean", "members": []},
        ],
        "total": 2,
        "done": True,
    }
    harness.server.queue("/series", scripted(200, conflicting))
    document = harness.run(
        {"alpha": _adapter(harness)},
        expect={"series_members": {"series": "The Empyrean", "members": [{"title": "Fourth Wing", "position": "1"}]}},
    )
    row = _row(document)
    present = _checks(row)["series.member[0].present"]
    _require(present["outcome"] == "ambiguous", f"expected ambiguous, got {present['outcome']}")
    _require("s1" in present["evidence"] and "s2" in present["evidence"], "evidence must list both ids")
    _require({record["provider_id"] for record in row["records"]} == {"s1", "s2"}, "both records must be kept")
    return Evidence(True, "ambiguous with both ids; nothing chosen")


@scenario("H12", "hard", "two runs and a replay are semantically identical")
def h12(harness: Harness) -> Evidence:
    _seed(harness)
    adapters = {"alpha": _adapter(harness)}
    first = harness.run(adapters, expect=MEMBER_EXPECT)
    second = harness.run(adapters, expect=MEMBER_EXPECT)
    _require(first["run"]["id"] != second["run"]["id"], "run ids should differ")
    _require(semantic_digest(first) == semantic_digest(second), "two identical runs must agree semantically")
    replay = ReplayHttp.from_result(first)
    replay_suite = __import__("metadata_lab.cases", fromlist=["Suite", "Case", "Reference"]).Suite(
        name="selftest",
        version=1,
        cases=tuple(
            __import__("metadata_lab.cases", fromlist=["Case"]).Case(
                id=case["id"],
                description=case.get("description") or "",
                reference=__import__("metadata_lab.cases", fromlist=["Reference"]).Reference(
                    source=(case.get("reference") or {}).get("source") or "selftest"
                ),
                input=case["input"],
                operations=tuple(Operation(name) for name in case["operations"]),
                expect=case["expect"],
                verified=bool(case.get("verified")),
            )
            for case in first["cases"]
        ),
        source="selftest",
        sha256="0" * 64,
    )
    replayed = run_suite(
        replay_suite,
        ["alpha"],
        None,
        http_factory=lambda pid: replay.for_provider(pid),
        adapters=adapters,
        run_mode=__import__("metadata_lab.model", fromlist=["RunMode"]).RunMode.REPLAY,
        replay_of=first["run"]["id"],
    )
    _require(semantic_digest(replayed) == semantic_digest(first), "replay must equal the original semantically")

    changed = {
        "series": [
            {
                "id": "s1",
                "name": "The Empyrean",
                "members": [{"id": "w1", "title": "Fourth Wing", "position": "2"}],
            }
        ],
        "total": 1,
        "done": True,
    }
    harness.server.reset()
    harness.server.queue("/series", scripted(200, changed))
    third = harness.run(adapters, expect=MEMBER_EXPECT)
    _require(semantic_digest(first) != semantic_digest(third), "a changed response must change the digest")
    _require(semantic_diff(first, third), "the changed row must be reported")
    return Evidence(True, "identical digests for repeat/replay; a changed response is reported")


@scenario("H13", "hard", "planted-fault canaries are each detected once")
def h13(harness: Harness) -> Evidence:
    adapters = {"alpha": _adapter(harness)}
    forbid = {"series_members": {**MEMBER_EXPECT["series_members"], "forbid": [{"title": "Threshing Day", "work_type": "novel"}]}}

    def run_with(series: Any, expect: Mapping[str, Any] = MEMBER_EXPECT) -> dict[str, Any]:
        harness.server.reset()
        harness.server.queue("/series", scripted(200, series))
        return harness.run(adapters, expect=expect)

    baseline = run_with(SERIES_OK)
    differences = [
        check
        for row in baseline["cases"][0]["results"]
        for check in row["checks"]
        if check["outcome"] in {"differs_from_reference", "ambiguous", "not_found", "incomplete"}
    ]
    _require(not differences, f"the fault-free baseline must be clean, got {differences}")

    def fault(members: list[dict[str, Any]], expect: Mapping[str, Any] = MEMBER_EXPECT) -> dict[str, Any]:
        series = {"series": [{"id": "s1", "name": "The Empyrean", "members": members}], "total": len(members), "done": True}
        return run_with(series, expect)

    base_members = SERIES_OK["series"][0]["members"]

    wrong_position = [dict(base_members[0]), {**base_members[1], "position": "4"}]
    document = fault(wrong_position)
    check = _checks(_row(document))["series.member[1].position"]
    _require(check["outcome"] == "differs_from_reference", "position fault not detected")
    _require("3.5" in str(check["expected"]) and "4" in str(check["observed"]), "both values must be named")

    missing_member = [base_members[0]]
    document = fault(missing_member)
    _require(_row(document)["outcome"] == "partial", "a missing member should be partial")

    extra_novel = [*base_members, {"id": "w3", "title": "Threshing Day", "position": "4", "work_type": "novel"}]
    document = fault(extra_novel, forbid)
    forbid_check = next(
        item for item in _row(document)["checks"] if item["name"] == "series.forbid[0]"
    )
    _require(forbid_check["outcome"] == "differs_from_reference", "forbidden novel not detected")

    wrong_author = [base_members[0], {**base_members[1], "authors": ["Someone Else"]}]
    expect_authors = {
        "series_members": {
            "series": "The Empyrean",
            "members": [{"title": "Threshing Day", "position": "3.5", "authors": ["Rebecca Yarros"]}],
        }
    }
    document = fault(wrong_author, expect_authors)
    _require(
        _checks(_row(document))["series.member[0].authors[0]"]["outcome"] in {"not_found", "ambiguous", "differs_from_reference"},
        "wrong member author not detected",
    )

    wrong_type = [base_members[0], {**base_members[1], "work_type": "novel"}]
    document = fault(wrong_type)
    _require(
        _checks(_row(document))["series.member[1].work_type"]["outcome"] == "differs_from_reference",
        "wrong classification not detected",
    )

    wrong_date = [base_members[0], {**base_members[1], "published": "2025-01-01"}]
    document = fault(wrong_date)
    _require(
        _checks(_row(document))["series.member[1].published"]["outcome"] == "differs_from_reference",
        "wrong date not detected",
    )

    unreleased = [base_members[0], {**base_members[1], "published": "2027-01-01"}]
    document = fault(unreleased)
    _require(
        _checks(_row(document))["series.member[1].published"]["outcome"] == "differs_from_reference",
        "an unverified future date not detected",
    )
    return Evidence(True, "baseline clean; 7 planted faults each detected with the right check")


# --- H14-H18: redaction, exports, replay, malformed input -------------------------


@scenario("H14", "hard", "redaction end to end: header, param, body, response, exception")
def h14(harness: Harness) -> Evidence:
    class Leaky(Transport):
        def __init__(self) -> None:
            self.first = True

        def send(self, method, url, *, params, headers, body, timeout):
            if self.first:
                self.first = False
                return TransportResponse(
                    200,
                    {},
                    json.dumps(
                        {
                            "series": [{"id": "s1", "name": f"leak {SECRET}", "members": []}],
                            "next": True,
                        }
                    ),
                )
            raise ConnectionError(f"transport rejected the request with token {SECRET}")

    harness.transports["leaky"] = Leaky()
    adapter = make_adapter(harness.server.base_url, "leaky", credential_env="MDLAB_SELFTEST_SECRET")
    import os

    os.environ["MDLAB_SELFTEST_SECRET"] = SECRET
    try:
        document = harness.run({"leaky": adapter})
    finally:
        os.environ.pop("MDLAB_SELFTEST_SECRET", None)
    paths = harness.write_outputs(document, "h14")
    texts = [json.dumps(document), paths["result"].read_text(encoding="utf-8"),
             paths["ai"].read_text(encoding="utf-8"), paths["report"].read_text(encoding="utf-8")]
    _require(all(SECRET not in text for text in texts), "the secret appears in an output file")
    row = _row(document, provider="leaky")
    _require(row["error"] is not None, "the second call should have failed")
    _require(SECRET not in row["error"]["message"], "the secret appears in an error message")
    return Evidence(True, "secret absent from document, both exports and the report")


@scenario("H15", "medium", "full export carries ids, params, pagination, stats and source URLs")
def h15(harness: Harness) -> Evidence:
    _seed(harness)
    document = harness.run({"alpha": _adapter(harness)}, expect=MEMBER_EXPECT)
    row = _row(document)
    _require(row["pagination"]["pages_fetched"] == 1, "pagination block missing")
    _require(set(row["stats"]) == {"calls", "retries", "throttled", "elapsed_ms"}, "stats block incomplete")
    _require(row["raw"], "raw exchanges missing")
    exchange = row["raw"][0]
    for key in ("method", "url", "params", "status", "elapsed_ms", "request_body"):
        _require(key in exchange, f"exchange missing {key}")
    record = row["records"][0]
    _require(record.get("source_url"), "normalised record missing source_url")
    _require(document["run"]["started_at"] and document["run"]["finished_at"], "run timestamps missing")
    return Evidence(True, "every block present on the row, exchange and record")


@scenario("H16", "medium", "AI export is compact, evidence-rich and untrusted-text safe")
def h16(harness: Harness) -> Evidence:
    _seed(harness)
    harness.server.reset()
    harness.server.queue(
        "/series",
        scripted(
            200,
            {
                "series": [
                    {
                        "id": "s1",
                        "name": "The Empyrean",
                        "members": [
                            {
                                "id": "w1",
                                "title": "Fourth Wing",
                                "position": "2",
                                "authors": ["Rebecca Yarros"],
                            }
                        ],
                    }
                ],
                "total": 1,
                "done": True,
            },
        ),
    )
    document = harness.run({"alpha": _adapter(harness)}, expect=MEMBER_EXPECT)
    exported = ai_export(full_export(document))
    payload = json.dumps(exported)
    _require("untrusted" in exported["note"].lower(), "the untrusted-text note is missing")
    _require('"raw"' not in payload, "raw bodies must be excluded")
    check = next(
        item
        for case in exported["cases"]
        for row in case["results"]
        for item in row["checks"]
        if item["name"] == "series.member[0].position"
    )
    _require(check["expected"] is not None and check["observed"] is not None, "expected/observed missing")
    _require(check["evidence"], "evidence missing")
    _require(max(len(value) for value in _strings(exported)) <= 201, "a string exceeds the 200-char limit")
    return Evidence(True, "note present, raw excluded, facts and evidence included, strings clipped")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


@scenario("H17", "hard", "replay uses the capture only, and a missing body is an error")
def h17(harness: Harness) -> Evidence:
    from ..model import BookRecord, Query

    class Posting(Transport):
        def send(self, method, url, *, params, headers, body, timeout):
            return TransportResponse(200, {}, json.dumps(BOOKS))

    class PostingAdapter(make_adapter(harness.server.base_url, "posting")):
        def book_by_title(self, ctx, q: Query):
            payload = ctx.post_json(f"{harness.server.base_url}/graphql", {"query": "A", "title": q.title})
            return [
                BookRecord(provider_id=raw["id"], title=raw["title"])
                for raw in payload.get("books") or []
            ]

    harness.transports["posting"] = Posting()
    adapters = {"posting": PostingAdapter}
    document = harness.run(adapters, operations=(Operation.BOOK_BY_TITLE,))
    _require(_row(document, provider="posting", operation="book_by_title")["outcome"] == "ok", "fixture run failed")

    replay = ReplayHttp.from_result(document)
    client = replay.for_provider("posting")
    ctx = client.context()
    ctx.post_json(f"{harness.server.base_url}/graphql", {"query": "A", "title": TITLE})
    try:
        ctx.post_json(f"{harness.server.base_url}/graphql", {"query": "B"})
    except ProviderError as exc:
        _require(exc.kind is ErrorKind.NETWORK and exc.message == "not in capture", "wrong miss behaviour")
    else:
        raise AssertionError("a body outside the capture must be an error")
    replayed = run_suite(
        __import__("metadata_lab.cases", fromlist=["Suite"]).Suite(
            name="selftest",
            version=1,
            cases=(
                __import__("metadata_lab.cases", fromlist=["Case"]).Case(
                    id=document["cases"][0]["id"],
                    description=document["cases"][0].get("description") or "",
                    reference=__import__("metadata_lab.cases", fromlist=["Reference"]).Reference(
                        source=(document["cases"][0].get("reference") or {}).get("source")
                        or "selftest"
                    ),
                    input=document["cases"][0]["input"],
                    operations=(Operation.BOOK_BY_TITLE,),
                    expect={},
                ),
            ),
            source="selftest",
            sha256="0" * 64,
        ),
        ["posting"],
        None,
        http_factory=lambda pid: replay.for_provider(pid),
        adapters=adapters,
        run_mode=__import__("metadata_lab.model", fromlist=["RunMode"]).RunMode.REPLAY,
        replay_of=document["run"]["id"],
    )
    _require(semantic_digest(replayed) == semantic_digest(document), "replay changed the semantics")
    return Evidence(True, "POST replay matched by body; a missing body is an error")


@scenario("H18", "hard", "malformed input fails clearly and never crashes the run")
def h18(harness: Harness) -> Evidence:
    from .. import cli

    tmp = harness.root / "h18"
    tmp.mkdir(exist_ok=True)
    bad_yaml = tmp / "bad.yaml"
    bad_yaml.write_text("suite: x\nversion: 1\ncases: [\n", encoding="utf-8")
    try:
        load_suite_file(bad_yaml)
    except CaseValidationError as exc:
        _require(str(bad_yaml) in str(exc), "the error should name the file")
    else:
        raise AssertionError("invalid YAML was accepted")

    empty = tmp / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    try:
        load_suite_file(empty)
    except CaseValidationError:
        pass
    else:
        raise AssertionError("an empty suite file was accepted")

    broken_result = tmp / "result.json"
    broken_result.write_text('{"cases": [', encoding="utf-8")
    import contextlib
    import io

    captured_err = io.StringIO()
    captured_out = io.StringIO()
    with contextlib.redirect_stderr(captured_err), contextlib.redirect_stdout(captured_out):
        code = cli.main(["replay", str(broken_result), "--out", str(tmp)])
    _require(code != 0, "a truncated result.json should be a usage error")
    _require("Traceback" not in captured_out.getvalue(), "a traceback leaked to stdout")
    _require(str(broken_result) in captured_err.getvalue(), "the error should name the file")

    shapes = [
        scripted(200, {"unexpected": 1}),
        scripted(200, raw='{"series": ['),
        scripted(200, raw=""),
        scripted(200, raw="<html>error</html>"),
    ]
    for index, response in enumerate(shapes):
        harness.server.reset()
        harness.server.queue("/series", response)
        document = harness.run(
            {"alpha": _adapter(harness, "alpha"), "beta": _adapter(harness, "beta")},
            providers=["alpha"],
        )
        row = _row(document)
        _require(row["outcome"] == "error", f"shape {index} should be an error, got {row['outcome']}")
        _require(row["error"]["kind"] == "parse", f"shape {index} should be parse, got {row['error']}")
    return Evidence(True, "bad YAML/empty/truncated result and 4 bad bodies all reported cleanly")


# --- H19-H24: cancel, politeness, report, extensibility, offline, expiry ----------


@scenario("H19", "hard", "cancel writes a partial result and resume finishes only the rest")
def h19(harness: Harness) -> Evidence:
    _seed(harness)

    class Interrupting(make_adapter(harness.server.base_url, "beta")):
        def series_members(self, ctx, q):
            raise KeyboardInterrupt

    done_calls: list[str] = []

    class Done(make_adapter(harness.server.base_url, "alpha")):
        def series_members(self, ctx, q):
            done_calls.append("alpha")
            return super().series_members(ctx, q)

    cancelled = harness.run({"alpha": Done, "beta": Interrupting})
    _require(cancelled["run"]["status"] == "cancelled", "the run should be marked cancelled")
    rows = {row["provider"]: row for row in cancelled["cases"][0]["results"]}
    _require(rows["alpha"]["outcome"] == "ok", "the finished row must stay intact")
    _require(rows["beta"]["outcome"] == "not_run" and rows["beta"]["reason"] == "cancelled",
             "the pending row must be not_run/cancelled")

    done_calls.clear()
    resumed = harness.run({"alpha": Done, "beta": _adapter(harness, "beta")}, resume=cancelled)
    _require(done_calls == [], "a finished row must not be re-run")
    resumed_rows = {row["provider"]: row for row in resumed["cases"][0]["results"]}
    _require(resumed_rows["alpha"].get("resumed") is True, "the reused row should be marked resumed")
    _require(resumed_rows["beta"]["outcome"] == "ok", "the pending provider should now run")
    _require(resumed["run"]["resumed_from"] == cancelled["run"]["id"], "resumed_from missing")
    return Evidence(True, "cancelled partial written; resume reused the finished row and ran the rest")


@scenario("H20", "hard", "politeness: interval, budget, Retry-After cap and retry bound")
def h20(harness: Harness) -> Evidence:
    _seed(harness)
    client = HttpClient(
        provider_id="alpha",
        transport=harness.transports.get("alpha"),
        min_interval_s=1.0,
        sleep=harness.clock.sleep,
        clock=harness.clock.clock,
    )
    ctx = client.context()
    ctx.get_json(f"{harness.server.base_url}/series")
    ctx.get_json(f"{harness.server.base_url}/series")
    _require(any(round(delay, 1) >= 1.0 for delay in harness.clock.slept), "min interval not honoured")

    harness.clock.slept.clear()
    harness.server.reset()
    harness.server.queue("/series", scripted(429, {}, headers={"Retry-After": "999"}), scripted(200, SERIES_OK))
    ctx.get_json(f"{harness.server.base_url}/series")
    _require(max(harness.clock.slept) == 20.0, f"Retry-After should cap at 20, got {harness.clock.slept}")

    budget_ctx = client.context(max_calls=1)
    budget_ctx.get_json(f"{harness.server.base_url}/series")
    try:
        budget_ctx.get_json(f"{harness.server.base_url}/series")
    except Exception as exc:
        _require(type(exc).__name__ == "BudgetExceeded", "budget should raise BudgetExceeded")
    else:
        raise AssertionError("the call budget was not enforced")

    harness.server.reset()
    harness.server.queue("/series", *[scripted(500, {}) for _ in range(3)])
    document = harness.run({"alpha": _adapter(harness)})
    _require(len(harness.server.calls("/series")) == 3, "retry bound exceeded")
    _require(_row(document)["outcome"] == "error", "the row should report the failure")
    return Evidence(True, "interval honoured, Retry-After capped at 20, budget and retry bound enforced")


@scenario("H21", "medium", "report escapes text, shows labels and ranks nothing")
def h21(harness: Harness) -> Evidence:
    harness.server.queue("/books", scripted(200, {"books": [{"id": "b1", "title": "<script>alert(1)</script>"}]}))
    document = harness.run({"alpha": _adapter(harness)}, operations=(Operation.BOOK_BY_TITLE,))
    page = render_report(document)
    _require("<script>alert(1)</script>" not in page, "provider text was not escaped")
    _require("&lt;script&gt;" in page, "escaped text missing")
    for label in ("ok", "not_run", "not_implemented"):
        _require(label in page, f"outcome label {label} missing")
    _require("Measurements" in page, "measurement table missing")
    _require("no composite score" in page and "capability profile" in page, "ranking disclaimer missing")
    _require("UNVERIFIED FIXTURE" in page, "unverified banner missing")
    _require("Harness status" in page, "harness banner missing")
    return Evidence(True, "escaped, labelled, disclaimed, banners shown")


@scenario("H22", "hard", "a new adapter file alone is discovered, listed and runs")
def h22(harness: Harness) -> Evidence:
    package_name = "mdlab_temp_providers"

    def _forget() -> None:
        for module_name in [name for name in sys.modules if name.startswith(package_name)]:
            sys.modules.pop(module_name, None)

    with tempfile.TemporaryDirectory(prefix="mdlab-ext-") as tmp:
        package = Path(tmp) / package_name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "zz_example.py").write_text(
            "from metadata_lab.providers.base import ProviderAdapter\n"
            "from metadata_lab.model import BookRecord\n"
            "\n"
            "class Example(ProviderAdapter):\n"
            "    id = 'zz-example'\n"
            "    display_name = 'ZZ Example'\n"
            "    def book_by_title(self, ctx, q):\n"
            "        return [BookRecord(provider_id='x1', title='Example')]\n",
            encoding="utf-8",
        )
        _forget()
        sys.path.insert(0, tmp)
        try:
            import importlib

            importlib.invalidate_caches()
            discovered = registry.discover(f"{package_name}")
            _require("zz-example" in discovered, "the new adapter was not discovered")
            document = harness.run(discovered, operations=(Operation.BOOK_BY_TITLE,))
            _require(
                document["providers"][0]["id"] == "zz-example",
                "the new adapter was not listed in the run",
            )
            _require(
                document["cases"][0]["results"][0]["outcome"] == "ok",
                "the new adapter did not run",
            )
            adapter = discovered["zz-example"]()
            _require(
                adapter.declared_support(Operation.SERIES_MEMBERS) == "not_implemented",
                "declared support should default to not_implemented",
            )
        finally:
            sys.path.remove(tmp)
            _forget()
    return Evidence(True, "discovered, listed and run with no other edits")


@scenario("H23", "hard", "the offline guard blocks every non-loopback connection")
def h23(harness: Harness) -> Evidence:
    # The runner already holds the guard open for the whole suite; prove it bites.
    try:
        socket.create_connection(("93.184.216.34", 80), timeout=0.2)
    except RuntimeError as exc:
        _require("non-loopback" in str(exc), "the guard message is unclear")
    except OSError as exc:
        raise AssertionError(f"a real network attempt escaped the guard: {exc}")
    else:
        raise AssertionError("a non-loopback connection was allowed")
    with socket.create_connection((harness.server.base_url.split("//")[1].split(":")[0],
                                   int(harness.server.base_url.rsplit(":", 1)[1])), timeout=1):
        pass
    return Evidence(True, "non-loopback blocked, loopback allowed")


@scenario("H24", "hard", "an expired reference is needs_review, never differs")
def h24(harness: Harness) -> Evidence:
    _seed(harness)
    expect = {
        "series_members": {
            "series": "The Empyrean",
            "members": [
                {
                    "title": "Threshing Day",
                    "position": "3.5",
                    "fact_type": "time_sensitive",
                    "valid_until": "2026-01-01",
                }
            ],
        }
    }
    adapters = {"alpha": _adapter(harness)}
    expired = harness.run(adapters, expect=expect, as_of="2026-11-14")
    check = _checks(_row(expired))["series.member[0]"]
    _require(check["outcome"] == "needs_review", f"expected needs_review, got {check['outcome']}")
    _require("reference expired" in check["evidence"], "the expiry must be named")
    _require(expired["run"]["as_of"] == "2026-11-14", "as_of not recorded")

    valid = harness.run(adapters, expect=expect, as_of="2025-06-01")
    check = _checks(_row(valid))["series.member[0].position"]
    _require(check["outcome"] == "ok", "before valid_until the reference should be scored")
    _require(valid["run"]["as_of"] == "2025-06-01", "as_of not recorded on the second run")
    return Evidence(True, "expired -> needs_review; before expiry -> scored; as_of recorded")


@scenario("H25", "hard", "measurement semantics: omission is not inaccuracy")
def h25(harness: Harness) -> Evidence:
    members = [
        {"id": f"w{index}", "title": f"Member {index}", "position": str(index), "work_type": "novel"}
        for index in range(1, 10)
    ]
    expected = {
        "series_members": {
            "series": "The Empyrean",
            "members": [
                {"title": f"Member {index}", "position": str(index)} for index in range(1, 10)
            ],
        }
    }

    def series_response(payload_members: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "series": [{"id": "s1", "name": "The Empyrean", "members": payload_members}],
            "total": len(payload_members),
        }

    six_with_positions = members[:6]
    all_without_positions = [{key: value for key, value in member.items() if key != "position"} for member in members]
    two_wrong = list(members[:6])
    two_wrong[1] = {**two_wrong[1], "position": "9"}
    two_wrong[4] = {**two_wrong[4], "position": "7"}

    harness.server.queue("/a/series", scripted(200, series_response(six_with_positions)))
    harness.server.queue("/b/series", scripted(200, series_response(all_without_positions)))
    harness.server.queue("/c/series", scripted(200, series_response(two_wrong)))
    adapters = {
        "aaa": make_adapter(harness.server.base_url, "aaa", path_prefix="/a"),
        "bbb": make_adapter(harness.server.base_url, "bbb", path_prefix="/b"),
        "ccc": make_adapter(harness.server.base_url, "ccc", path_prefix="/c"),
    }
    document = harness.run(adapters, expect=expected)
    by_provider = document["measurements"]["by_provider"]

    a = by_provider["aaa"]
    member_coverage = a["aspects"]["series.member[].present"]
    _require(
        member_coverage["ok"] == 6 and member_coverage["expected"] == 9,
        f"A member coverage must be 6/9, got {member_coverage}",
    )
    _require(
        a["ordering"]["ok"] == 6 and a["ordering"]["reported"] == 6,
        f"A ordering accuracy must be 6/6, got {a['ordering']}",
    )
    _require(
        a["ordering"]["expected"] == 9 and a["ordering"]["omitted"] == 3,
        f"A position coverage must be 6/9, got {a['ordering']}",
    )

    b = by_provider["bbb"]
    b_members = b["aspects"]["series.member[].present"]
    _require(b_members["ok"] == 9 and b_members["expected"] == 9, f"B coverage {b_members}")
    _require(b["ordering"]["reported"] == 0, f"B ordering must have no reported values: {b['ordering']}")
    _require(b["ordering"]["expected"] == 9, f"B position coverage must be 0/9: {b['ordering']}")
    _require(
        b["ordering"]["omitted"] == 9 and b["ordering"]["ok"] == 0,
        f"B ordering should be all omitted: {b['ordering']}",
    )

    c = by_provider["ccc"]
    _require(
        c["ordering"]["ok"] == 4 and c["ordering"]["differs"] == 2 and c["ordering"]["reported"] == 6,
        f"C ordering must be 4/6 with 2 differs: {c['ordering']}",
    )
    _require(c["ordering"]["expected"] == 9, f"C position coverage must be 6/9: {c['ordering']}")

    # Omission never lowers accuracy: A is 6/6 on ordering although it omitted 3.
    _require(
        a["ordering"]["ok"] == a["ordering"]["reported"],
        "an omitted position must not enter the accuracy denominator",
    )
    return Evidence(
        True,
        "A 6/9 coverage, 6/6 ordering, 6/9 position coverage; B 9/9, 0/0 ordering, 0/9; C 4/6 with 2 differs",
    )


@scenario("H26", "hard", "unresolved references are incomplete, never not_found")
def h26(harness: Harness) -> Evidence:
    without_authors = {
        "series": [
            {
                "id": "s1",
                "name": "The Empyrean",
                "members": [{"id": "w1", "title": "Fourth Wing", "position": "1"}],
            }
        ],
        "total": 1,
    }
    with_authors = {
        "series": [
            {
                "id": "s1",
                "name": "The Empyrean",
                "members": [
                    {
                        "id": "w1",
                        "title": "Fourth Wing",
                        "position": "1",
                        "authors": ["Rebecca Yarros"],
                    }
                ],
            }
        ],
        "total": 1,
    }
    harness.server.queue("/unfollowed/series", scripted(200, without_authors))
    harness.server.queue("/followed/series", scripted(200, with_authors))
    adapters = {
        "unfollowed": make_adapter(
            harness.server.base_url,
            "unfollowed",
            path_prefix="/unfollowed",
            on_call=lambda ctx, _q: ctx.note_unresolved("authors", 1),
        ),
        "followed": make_adapter(harness.server.base_url, "followed", path_prefix="/followed"),
    }
    expect = {
        "series_members": {
            "series": "The Empyrean",
            "members": [
                {"title": "Fourth Wing", "position": "1", "authors": ["Rebecca Yarros"]}
            ],
        }
    }
    document = harness.run(adapters, expect=expect)
    rows = {row["provider"]: row for row in document["cases"][0]["results"]}

    unfollowed = _checks(rows["unfollowed"])["series.member[0].authors[0]"]
    _require(unfollowed["outcome"] == "incomplete", f"expected incomplete, got {unfollowed['outcome']}")
    _require(
        unfollowed["evidence"] == "adapter did not resolve",
        f"evidence must say the adapter did not resolve, got {unfollowed['evidence']!r}",
    )
    followed = _checks(rows["followed"])["series.member[0].authors[0]"]
    _require(followed["outcome"] == "ok", f"followed references should be ok, got {followed['outcome']}")
    _require(rows["unfollowed"]["outcome"] == "incomplete", "the op should be incomplete, not not_found")
    return Evidence(True, "unfollowed -> incomplete ('adapter did not resolve'); followed -> ok")


__all__ = ["BOOKS", "MEMBER_EXPECT", "SERIES_OK", "TITLE"]
