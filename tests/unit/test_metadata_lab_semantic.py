"""Semantic comparison of result documents (H04/H12/H17 support)."""

from __future__ import annotations

import copy
import json

from metadata_lab.semantic import semantic_diff, semantic_digest, semantic_view


def _document(**overrides) -> dict:
    document = {
        "schema": "metadata-lab.result/1",
        "run": {
            "id": "run-1",
            "started_at": "2026-11-14T10:00:00Z",
            "finished_at": "2026-11-14T10:00:05Z",
            "mode": "live",
            "replay_of": None,
            "status": "complete",
            "as_of": "2026-11-14",
            "tool_version": "0.1.0",
            "matcher_version": "2",
            "git_revision": "abc1234",
            "python": "3.13.7",
        },
        "suite": {"name": "s", "version": 1, "source": "memory", "sha256": "0" * 64},
        "providers": [{"id": "fake", "declared": {}, "configured": True}],
        "cases": [
            {
                "id": "c1",
                "verified": False,
                "results": [
                    {
                        "provider": "fake",
                        "operation": "book_by_title",
                        "mode": "independent",
                        "outcome": "ok",
                        "elapsed_ms": 12,
                        "truncated": False,
                        "checks": [{"name": "book.title", "outcome": "ok"}],
                        "records": [{"kind": "book", "title": "Fourth Wing"}],
                        "raw": [{"method": "GET", "url": "https://example.org", "body": {"x": 1}}],
                        "pagination": {"pages_fetched": 1},
                        "duplicates": [],
                        "stats": {"calls": 1, "retries": 0, "throttled": 0, "elapsed_ms": 12},
                    }
                ],
            }
        ],
        "matrix": {"fake": {"book_by_title": {"ok": 1, "total": 1}}},
        "matrix_checks": {"fake": {"book_by_title": {"book.title": {"ok": 1, "total": 1}}}},
    }
    document.update(overrides)
    return document


def test_semantic_view_drops_volatile_fields_and_raw_bodies():
    view = semantic_view(_document())
    row = view["cases"][0]["results"][0]

    assert "id" not in view["run"]
    assert "started_at" not in view["run"]
    assert "finished_at" not in view["run"]
    assert "git_revision" not in view["run"]
    assert "raw" not in row
    assert row["elapsed_ms"] == 0
    assert row["stats"]["elapsed_ms"] == 0
    # Semantic content survives.
    assert row["records"][0]["title"] == "Fourth Wing"
    assert view["run"]["matcher_version"] == "2"


def test_two_runs_that_only_differ_in_volatile_fields_have_the_same_digest():
    first = _document()
    second = copy.deepcopy(first)
    second["run"]["id"] = "run-2"
    second["run"]["started_at"] = "2026-11-14T11:00:00Z"
    second["run"]["finished_at"] = "2026-11-14T11:00:09Z"
    second["run"]["git_revision"] = "def5678"
    second["cases"][0]["results"][0]["elapsed_ms"] = 999
    second["cases"][0]["results"][0]["stats"]["elapsed_ms"] = 999
    second["cases"][0]["results"][0]["raw"] = []

    assert semantic_digest(first) == semantic_digest(second)


def test_a_changed_outcome_changes_the_digest_and_the_diff():
    first = _document()
    second = copy.deepcopy(first)
    second["cases"][0]["results"][0]["outcome"] = "partial"

    assert semantic_digest(first) != semantic_digest(second)
    changes = semantic_diff(first, second)
    assert changes == [
        {
            "case": "c1",
            "provider": "fake",
            "operation": "book_by_title",
            "before_outcome": "ok",
            "after_outcome": "partial",
        }
    ]


def test_semantic_view_is_json_serialisable_and_stable():
    first = json.dumps(semantic_view(_document()), sort_keys=True)
    second = json.dumps(semantic_view(_document()), sort_keys=True)

    assert first == second


def test_replay_provenance_does_not_change_the_digest():
    live = _document()
    replay = copy.deepcopy(live)
    replay["run"]["mode"] = "replay"
    replay["run"]["replay_of"] = "run-1"
    replay["run"]["harness"] = {"status": "validated", "selftest_id": "x", "digest": "y"}

    assert semantic_digest(live) == semantic_digest(replay)


def test_run_status_stays_semantic():
    live = _document()
    cancelled = copy.deepcopy(live)
    cancelled["run"]["status"] = "cancelled"

    assert semantic_digest(live) != semantic_digest(cancelled)
