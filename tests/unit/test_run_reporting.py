"""What a run says about itself afterwards: which log folders belong to it, and which commits it tested."""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.suites import Suite
from family_librarian_lab import commands

_RUN = "20261127T210512843705Z-suites-all"


def _write_run(root, *failed: str, finished: str | None) -> None:
    run = root / _RUN
    run.mkdir()
    summary = {"suites": [{"suite": "artifact-release", "result": "results-artifact-release.json"}]}
    if finished:
        summary["completed_at_utc"] = finished
    (run / "results-suite-run.json").write_text(json.dumps(summary))
    (run / "results-artifact-release.json").write_text(json.dumps(
        {"results": [{"name": name, "status": "fail", "message": "broke"} for name in failed]}))
    if not finished:
        written = datetime(2026, 11, 27, 22, 23, 47, tzinfo=UTC).timestamp()
        os.utime(run / "results-suite-run.json", (written, written))


def _case_folder(root, stamp: str, case: str):
    folder = root / f"{stamp}Z-family-librarian-lab-{case}-{stamp.replace('T', '')}000000"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("finished", ["20261127T222347000000Z", None], ids=["recorded end", "older run"])
def test_only_log_folders_this_run_created_are_linked(monkeypatch, tmp_path, capsys, finished) -> None:
    _write_run(tmp_path, "AR-03", "AR-04", finished=finished)
    _case_folder(tmp_path, "20261127T163900", "ar-04")  # an earlier run
    mine = _case_folder(tmp_path, "20261127T221747", "ar-03")
    _case_folder(tmp_path, "20261127T230000", "ar-03")  # a later run
    _case_folder(tmp_path, "20261127T230100", "ar-04")
    monkeypatch.setattr(commands, "RESULTS_ROOT", tmp_path)

    commands.handle_report_failed(SimpleNamespace(from_run=_RUN, include_skipped=False), None)

    output = capsys.readouterr().out
    ar_03, ar_04 = output.split("[artifact-release] FAIL AR-04")
    assert f"logs: {mine}" in ar_03
    assert "this run saved none for this case" in ar_04
    assert "20261127T163900" not in output and "20261127T23" not in output


def test_a_case_id_does_not_match_a_longer_one_and_the_newest_folder_wins(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(commands, "RESULTS_ROOT", tmp_path)
    _case_folder(tmp_path, "20261127T221000", "cwa-s-02")
    newest = _case_folder(tmp_path, "20261127T221500", "cwa-s-02-key")
    _case_folder(tmp_path, "20261127T222000", "cwa-s-020")

    found = commands._find_case_artifact_dir("CWA-S-02", "20261127T210512", "20261127T222347")

    assert found == newest


@pytest.mark.parametrize("replacements", [0, 2])
def test_the_run_summary_names_both_commits_and_any_replaced_cwa(monkeypatch, tmp_path, capsys, replacements) -> None:
    def preflight(*_args) -> bool:
        commands._SHARED_CWA_REPLACEMENTS.extend(
            tmp_path / f"stamp{number}-shared-cwa-diagnostics" for number in range(replacements))
        return False

    for name, value in {
        "_describe_run_plan": lambda _: SimpleNamespace(confirm=lambda **_: True),
        "discover_suites": lambda _: [Suite("selected")],
        "select_suites": lambda suites, **_: suites,
        "_load_lab_env": lambda: {},
        "_check_no_conflicting_containers": lambda _: None,
        "ensure_shared_postgres": lambda _: None,
        "ensure_shared_cwa": lambda _: None,
        "ensure_shared_clamav": lambda: {},
        "_run_prerequisites": preflight,
        "_stop_shared_cwa": lambda _: None,
        "_stop_shared_postgres": lambda _: None,
        "_stop_shared_clamav": lambda: None,
        "_write_se_lab_report_record": lambda **_: None,
        "RESULTS_ROOT": tmp_path,
        # Left over from an earlier run in the same process: must not be reported again.
        "_SHARED_CWA_REPLACEMENTS": [tmp_path / "earlier-shared-cwa-diagnostics"],
    }.items():
        monkeypatch.setattr(commands, name, value)
    monkeypatch.setattr(commands.lab_common, "run_lock", lambda **_: nullcontext())
    monkeypatch.setattr(commands.lab_common, "is_git_checkout", lambda _: True)
    monkeypatch.setattr(commands.lab_common, "repo_dir", lambda: tmp_path)
    monkeypatch.setattr(commands.lab_common, "repo_current_branch", lambda: "feature/example")
    monkeypatch.setattr(commands.lab_common, "repo_head_commit", lambda **_: "product1")
    monkeypatch.setattr(commands.lab_common, "git_commit", lambda *_, **__: "labrepo2")
    arguments = SimpleNamespace(target=None, skip_build=True, yes=True, test_group="all", case=None, keep=False)

    commands.handle_run(arguments, None)

    output = capsys.readouterr().out
    lines = output.splitlines()
    assert any("Product commit" in line and "product1" in line for line in lines), output
    assert any("Lab commit" in line and "labrepo2" in line for line in lines), output
    run_directory = next(tmp_path.glob("*-suites-all"))
    summary = json.loads((run_directory / "results-suite-run.json").read_text())
    assert len(summary["shared_cwa_replacements"]) == replacements
    assert summary["completed_at_utc"][:15] >= run_directory.name[:15]
    assert ("Shared CWA replaced" in output) is bool(replacements)
    assert "earlier-shared-cwa-diagnostics" not in output
