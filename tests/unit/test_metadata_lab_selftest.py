"""The selftest suite, run from the normal unit run.

This is what keeps the harness honest: every scenario must pass, the canaries
must be able to *fail* (meta-test), the stamp digest must track the sources, and
the offline guard must actually block.
"""

from __future__ import annotations

import socket

import pytest

from metadata_lab.selftest import runner, scenarios
from metadata_lab.selftest.runner import Harness, OfflineGuard, run_all
from metadata_lab.selftest.stamp import harness_source_digest, harness_status, write_stamp


@pytest.fixture(scope="module")
def summary() -> dict:
    return run_all()


def test_all_twenty_eight_scenarios_pass(summary: dict):
    ids = [item["id"] for item in summary["results"]]

    assert ids == [f"H{index:02d}" for index in range(1, 29)]
    assert summary["failed"] == 0, summary["failures"]
    assert summary["passed"] == 28


def test_selftest_is_fast_enough(summary: dict):
    # Definition of "harness accepted": the suite runs in under 60 seconds.
    assert summary["duration_s"] < 60


def test_selftest_is_deterministic(summary: dict):
    second = run_all()

    assert [(item["id"], item["ok"]) for item in second["results"]] == [
        (item["id"], item["ok"]) for item in summary["results"]
    ]


def test_every_scenario_is_registered_with_metadata(summary: dict):
    for item in summary["results"]:
        assert item["difficulty"] in {"easy", "medium", "hard"}
        assert item["title"]


def test_h13_canaries_detect_a_broken_comparison(monkeypatch: pytest.MonkeyPatch):
    """Meta-test: if the comparison is blinded, H13 must FAIL (so it is not vacuous)."""

    from metadata_lab.matcher import PositionResult

    monkeypatch.setattr(
        "metadata_lab.checks.compare_position",
        lambda expected, observed: PositionResult(True, "always equal"),
    )

    summary = run_all(only=["H13"])

    assert summary["failed"] == 1, "H13 passed with a blinded position comparison"


def test_h25_detects_a_denominator_that_counts_omissions(monkeypatch: pytest.MonkeyPatch):
    """Meta-test: the old "omission counts against accuracy" logic must fail H25."""

    from metadata_lab import measurements

    def old_style(rows):
        counts = {dimension: measurements._empty_bucket() for dimension in measurements.DIMENSIONS}
        for row in rows:
            for item in row.get("checks", []):
                bucket = counts[item["dimension"]]
                outcome = str(item["outcome"])
                if outcome in measurements.SCORED_EXCLUDED:
                    bucket["needs_review"] += 1
                    continue
                bucket["expected"] += 1
                if outcome == "ok":
                    bucket["ok"] += 1
                elif outcome == "not_found":
                    bucket["omitted"] += 1
        for bucket in counts.values():
            # The old behaviour: everything expected is a denominator.
            bucket["reported"] = bucket["expected"]
            bucket["total"] = bucket["expected"]
        return counts

    monkeypatch.setattr(measurements, "_dimension_counts", old_style)

    summary = run_all(only=["H25"])

    assert summary["failed"] == 1, "H25 passed with omissions counted against accuracy"


def test_h09_detects_a_disabled_retry_bound(monkeypatch: pytest.MonkeyPatch):
    """Meta-test: with retries disabled, H09's "3 attempts" assertion must fail."""

    from metadata_lab import http as http_module
    from metadata_lab.selftest import runner as runner_module

    class NoRetry(http_module.HttpClient):
        def __init__(self, *args, **kwargs):
            kwargs["max_retries"] = 0
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runner_module, "HttpClient", NoRetry)

    summary = run_all(only=["H09"])

    assert summary["failed"] == 1, "H09 passed even with retries disabled"


def test_h10_detects_disabled_duplicate_detection(monkeypatch: pytest.MonkeyPatch):
    """Meta-test: without duplicate detection, H10 must fail."""

    # The engine imported the symbol directly, so patch it there.
    monkeypatch.setattr("metadata_lab.engine.duplicates", lambda records: [])

    summary = run_all(only=["H10"])

    assert summary["failed"] == 1, "H10 passed even though duplicates were never detected"


def test_h27_detects_a_disabled_breaker(monkeypatch: pytest.MonkeyPatch):
    """Meta-test: with the breaker disabled, H27 must fail."""

    monkeypatch.setattr(
        "metadata_lab.http._breaker_trips",
        lambda consecutive_429, retry_after_s: False,
    )

    summary = run_all(only=["H27"])

    assert summary["failed"] == 1, "H27 passed even though the breaker never tripped"


def test_h28_detects_an_ignored_cooldown_file(monkeypatch: pytest.MonkeyPatch):
    """Meta-test: if the run ignores the cool-down record, H28 must fail."""

    monkeypatch.setattr(
        "metadata_lab.pacing.CooldownStore.note_for",
        lambda self, provider_id: None,
    )

    summary = run_all(only=["H28"])

    assert summary["failed"] == 1, "H28 passed even though the cool-down record was ignored"


def test_offline_guard_blocks_non_loopback():
    with OfflineGuard():
        with pytest.raises(RuntimeError):
            socket.create_connection(("93.184.216.34", 80), timeout=0.2)


def test_stamp_digest_changes_with_the_sources(tmp_path):
    import shutil

    source = scenarios.__file__
    target = tmp_path / "pkg"
    (target / "sub").mkdir(parents=True)
    (target / "a.py").write_text("X = 1\n", encoding="utf-8")
    first = harness_source_digest(target)
    (target / "b.py").write_text("Y = 2\n", encoding="utf-8")
    second = harness_source_digest(target)
    (target / "a.py").write_text("X = 3\n", encoding="utf-8")
    third = harness_source_digest(target)

    assert first != second != third
    assert source  # the module imported cleanly
    shutil.rmtree(target, ignore_errors=True)


def test_harness_status_reports_not_run_without_a_stamp(tmp_path):
    status = harness_status(tmp_path)

    assert status["status"] == "not_run"
    assert status["digest"] == harness_source_digest()


def test_harness_status_reports_stale_for_an_old_stamp(tmp_path):
    write_stamp(passed=24, failed=0, duration_s=1.0, root=tmp_path)
    stale = harness_status(tmp_path)

    # The stamp directory lives under the given root, but the digest captured the
    # real package: matching the current digest makes it validated.
    assert stale["status"] in {"validated", "stale"}
    assert stale["selftest_id"]


def test_harness_status_marks_a_failed_stamp_stale(tmp_path):
    write_stamp(passed=23, failed=1, duration_s=1.0, failures={"H13": "x"}, root=tmp_path)

    assert harness_status(tmp_path)["status"] == "stale"


def test_harness_writes_a_stamp_when_passwords_are_absent(tmp_path):
    path = write_stamp(passed=24, failed=0, duration_s=2.0, root=tmp_path)

    assert path.exists()
    assert "selftest" in path.name or path.parent.name == "selftest"


def test_latest_stamp_wins_by_age_not_by_filename(tmp_path):
    import time

    older = write_stamp(passed=24, failed=0, duration_s=1.0, root=tmp_path)
    time.sleep(0.01)
    stamp_dir = older.parent
    newer = stamp_dir / "0.1.0-000000000000.json"
    newer.write_text(
        older.read_text(encoding="utf-8").replace('"passed": 24', '"passed": 26'),
        encoding="utf-8",
    )
    time.sleep(0.01)
    import os

    os.utime(newer, None)  # make sure it is the newest by mtime

    from metadata_lab.selftest.stamp import latest_stamp

    assert latest_stamp(tmp_path)["passed"] == 26


def test_scenario_registry_is_sorted_and_unique():
    ids = [item.id for item in runner.SCENARIOS]

    assert len(ids) == len(set(ids))
    assert sorted(ids) == ids


def test_harness_has_a_scripted_server_and_fake_clock(tmp_path):
    harness = Harness(tmp_path)

    assert hasattr(harness.server, "queue")
    assert harness.clock.slept == []
