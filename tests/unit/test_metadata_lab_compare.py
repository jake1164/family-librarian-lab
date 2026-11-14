"""compare: provider/outcome/timing changes between two result documents."""

from __future__ import annotations

import copy

from metadata_lab.compare import compare


def _document(**overrides) -> dict:
    document = {
        "schema": "metadata-lab.result/1",
        "run": {"id": "run-1", "mode": "live", "status": "complete"},
        "suite": {"name": "s", "version": 1},
        "providers": [{"id": "alpha"}, {"id": "beta"}],
        "cases": [
            {
                "id": "c1",
                "reference": {"source": "ref", "verified_on": None},
                "verified": False,
                "expect": {"series_members": {"series": "The Empyrean"}},
                "operations": ["series_members"],
                "results": [
                    {
                        "provider": "alpha",
                        "operation": "series_members",
                        "mode": "independent",
                        "outcome": "ok",
                        "elapsed_ms": 100,
                        "checks": [],
                        "records": [],
                        "truncated": False,
                    },
                    {
                        "provider": "beta",
                        "operation": "series_members",
                        "mode": "independent",
                        "outcome": "partial",
                        "elapsed_ms": 200,
                        "checks": [],
                        "records": [],
                        "truncated": False,
                    },
                ],
            }
        ],
        "matrix": {},
    }
    document.update(overrides)
    return document


def test_identical_semantic_documents_report_no_changes():
    result = compare(_document(), _document())

    assert result["semantic_equal"] is True
    assert result["changes"] == []


def test_timing_only_change_is_reported_but_semantics_stay_equal():
    first = _document()
    second = copy.deepcopy(first)
    second["cases"][0]["results"][0]["elapsed_ms"] = 400  # +300%

    result = compare(first, second)

    assert result["semantic_equal"] is True
    kinds = [change["kind"] for change in result["changes"]]
    assert kinds == ["elapsed_changed"]
    assert result["changes"][0]["provider"] == "alpha"


def test_small_timing_change_is_ignored():
    first = _document()
    second = copy.deepcopy(first)
    second["cases"][0]["results"][0]["elapsed_ms"] = 120  # +20%

    assert compare(first, second)["changes"] == []


def test_outcome_change_is_reported():
    first = _document()
    second = copy.deepcopy(first)
    second["cases"][0]["results"][1]["outcome"] = "ok"

    changes = compare(first, second)["changes"]

    assert changes == [
        {
            "kind": "outcome_changed",
            "case": "c1",
            "provider": "beta",
            "operation": "series_members",
            "before_outcome": "partial",
            "after_outcome": "ok",
        }
    ]


def test_provider_added_and_removed():
    first = _document()
    second = copy.deepcopy(first)
    second["providers"] = [{"id": "alpha"}, {"id": "gamma"}]
    second["cases"][0]["results"] = [
        row for row in second["cases"][0]["results"] if row["provider"] != "beta"
    ]

    kinds = {(change["kind"], change.get("provider")) for change in compare(first, second)["changes"]}

    assert ("new_provider", "gamma") in kinds
    assert ("removed_provider", "beta") in kinds
    assert ("removed_row", "beta") in kinds


def test_reference_change_is_reported():
    first = _document()
    second = copy.deepcopy(first)
    second["cases"][0]["reference"]["verified_on"] = "2026-11-14"
    second["cases"][0]["verified"] = True

    kinds = [change["kind"] for change in compare(first, second)["changes"]]

    assert "reference_changed" in kinds
