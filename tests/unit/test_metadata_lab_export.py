"""Full/AI export, the self-contained report, and capture replay."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from metadata_lab.cases import Case, Reference, Suite
from metadata_lab.engine import run_suite
from metadata_lab.export import SecretLeakError, ai_export, full_export
from metadata_lab.http import HttpClient, TransportResponse
from metadata_lab.model import BookRecord, Operation, RunMode, SeriesMember, SeriesRecord
from metadata_lab.providers.base import ProviderAdapter
from metadata_lab.replay import ReplayHttp
from metadata_lab.report import render_report
from metadata_lab.semantic import semantic_digest

LONG_TEXT = "x" * 500


def _document() -> dict[str, Any]:
    """Two providers that disagree about one position, plus risky text."""

    def row(provider: str, position: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "operation": "series_members",
            "mode": "independent",
            "outcome": "ok",
            "reference_verified": False,
            "elapsed_ms": 5,
            "truncated": False,
            "notes": [],
            "error": None,
            "checks": [
                {
                    "name": "series.member[0].position",
                    "outcome": "ok" if position == "1" else "differs_from_reference",
                    "expected": "1",
                    "observed": position,
                    "match_kind": None,
                    "evidence": f"position {position}",
                    "dimension": "ordering",
                },
                {
                    "name": "book.min_fields",
                    "outcome": "differs_from_reference",
                    "expected": ["publisher"],
                    "observed": {"record": "Fourth Wing", "missing": ["publisher"]},
                    "match_kind": None,
                    "evidence": "missing field(s): publisher",
                    "dimension": "accuracy",
                },
            ],
            "records": [
                {
                    "kind": "book",
                    "record_type": "book",
                    "title": "<script>alert(1)</script>",
                    "description": LONG_TEXT,
                }
            ],
            "raw": [{"method": "GET", "url": "https://example.org", "body": {"secret": "x"}}],
            "pagination": {"pages_fetched": 1, "retrieved": 1, "truncated": False},
            "duplicates": [],
            "stats": {"calls": 1, "retries": 0, "throttled": 0, "elapsed_ms": 5},
        }

    return {
        "schema": "metadata-lab.result/1",
        "run": {
            "id": "run-1",
            "mode": "live",
            "status": "complete",
            "as_of": "2026-11-14",
            "harness": {"status": "not_run", "selftest_id": None, "digest": None},
            "tool_version": "0.1.0",
            "matcher_version": "2",
            "replay_of": None,
            "semantic_digest": "abc",
        },
        "suite": {"name": "demo", "version": 1, "source": "memory", "sha256": "0" * 64},
        "providers": [
            {"id": "alpha", "display_name": "Alpha", "verified_live": True, "configured": True},
            {"id": "beta", "display_name": "Beta", "verified_live": False, "configured": True},
        ],
        "cases": [
            {
                "id": "c1",
                "description": LONG_TEXT,
                "tier": "easy",
                "tags": ["demo"],
                "reference": {"source": "somewhere", "verified_on": None},
                "verified": False,
                "input": {"series": "The Empyrean"},
                "expect": {"series_members": {"members": [{"title": "Fourth Wing"}]}},
                "results": [row("alpha", "1"), row("beta", "3.5")],
            }
        ],
        "matrix": {
            "alpha": {"series_members": {"ok": 1, "total": 1}},
            "beta": {"series_members": {"differs_from_reference": 1, "total": 1}},
        },
        "matrix_checks": {},
        "measurements": {"by_provider": {}, "by_case": {}},
    }


def test_ai_export_carries_the_untrusted_note():
    assert "untrusted" in ai_export(_document())["note"].lower()


def test_ai_export_excludes_raw_bodies_and_descriptions():
    exported = ai_export(_document())
    payload = json.dumps(exported)

    assert "https://example.org" not in payload
    assert '"raw"' not in payload
    assert LONG_TEXT not in payload
    assert '"description"' not in payload


def test_ai_export_truncates_long_strings():
    exported = ai_export(_document())

    def walk(value):
        if isinstance(value, str):
            assert len(value) <= 201, value[:40]
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(exported)


def test_ai_export_lists_disagreements_and_missing_fields():
    exported = ai_export(_document())
    case = exported["cases"][0]

    assert case["disagreements"][0]["check"] == "series.member[0].position"
    assert set(case["disagreements"][0]["providers"]) == {"alpha", "beta"}
    assert case["missing_fields"][0]["missing"] == ["publisher"]


def test_ai_export_has_capability_counts_without_a_score():
    exported = ai_export(_document())
    payload = json.dumps(exported).lower()

    assert exported["capability"]["alpha"]["series_members"]["ok"] == 1
    assert "score" not in payload
    assert "rank" not in payload.replace("ranking", "")


def test_full_export_refuses_a_document_containing_a_credential():
    document = _document()
    document["cases"][0]["description"] = "leaked SECRET-12345"

    with pytest.raises(SecretLeakError) as info:
        full_export(document, ["SECRET-12345"])

    assert "SECRET-12345" not in str(info.value)


def test_full_export_passes_clean_documents_through():
    document = _document()

    assert full_export(document, ["SECRET-12345"]) == document


def test_report_escapes_provider_text():
    page = render_report(_document())

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_report_states_that_it_is_a_capability_profile():
    page = render_report(_document())

    assert "capability profile" in page
    assert "no composite score" in page
    assert "recommendation" in page


def test_report_shows_banners_and_outcome_labels():
    page = render_report(_document())

    assert "UNVERIFIED FIXTURE" in page
    assert "Harness status" in page  # harness was not_run
    for label in ("ok", "differs_from_reference"):
        assert label in page


def test_report_is_self_contained():
    page = render_report(_document())

    assert "<style>" in page
    assert "src=\"http" not in page
    assert "href=\"http" not in page


# --- replay of a real capture ------------------------------------------------------


class Capturing(ProviderAdapter):
    id: ClassVar[str] = "capturing"
    display_name: ClassVar[str] = "Capturing"

    def series_members(self, ctx, q):
        payload = ctx.get_json("https://api.example.org/series", params={"name": q.series})
        return [
            SeriesRecord(
                provider_id="s1",
                name=payload.get("name", ""),
                members=[
                    SeriesMember(title=member["title"], position=member.get("position"))
                    for member in payload.get("members", [])
                ],
            )
        ]


class OneResponse:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, method, url, *, params, headers, body, timeout):
        self.calls += 1
        return TransportResponse(
            200,
            {},
            json.dumps(
                {"name": "The Empyrean", "members": [{"title": "Fourth Wing", "position": "1"}]}
            ),
        )


def _suite() -> Suite:
    case = Case(
        id="c1",
        description="replay case",
        reference=Reference(source="memory"),
        input={"series": "The Empyrean", "author": "Rebecca Yarros"},
        operations=(Operation.SERIES_MEMBERS,),
        expect={"series_members": {"members": [{"title": "Fourth Wing", "position": "1"}]}},
    )
    return Suite(name="replay", version=1, cases=(case,), source="memory", sha256="0" * 64)


def test_replay_reproduces_the_semantic_digest_without_network():
    adapters = {"capturing": Capturing}
    transport = OneResponse()
    original = run_suite(
        _suite(),
        ["capturing"],
        None,
        http_factory=lambda pid: HttpClient(
            provider_id=pid, transport=transport, min_interval_s=0.0
        ),
        adapters=adapters,
    )
    assert transport.calls == 1
    assert original["cases"][0]["results"][0]["outcome"] == "ok"

    replay = ReplayHttp.from_result(original)
    replayed = run_suite(
        _suite(),
        ["capturing"],
        None,
        http_factory=lambda pid: replay.for_provider(pid),
        adapters=adapters,
        run_mode=RunMode.REPLAY,
        replay_of=original["run"]["id"],
    )

    assert replayed["run"]["mode"] == "replay"
    assert semantic_digest(replayed) == semantic_digest(original)
    assert replayed["cases"][0]["results"][0]["outcome"] == "ok"
