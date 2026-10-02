"""Unit coverage for the build-revision stamp and report: what the lab passes to a provider's build, and
how it flags an image that was not built from the checkout."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import commands  # noqa: E402

CHECKOUT = "b88d875693910f866898147854b657b7cbcfc1c8"


def _provider(**overrides) -> commands.ProviderConfig:
    values = dict(
        name="p", compose_file="x", internal_url="http://p", app_service="p-app",
        source_dir_env="FAMILY_LIBRARIAN_P_SOURCE_DIR",
    )
    values.update(overrides)
    return commands.ProviderConfig(**values)


def test_stamp_is_named_from_source_dir_env_and_uses_the_commit_date(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(commands.lab_common, "git_commit", lambda _path, short=False: CHECKOUT)
    monkeypatch.setattr(
        commands.lab_common, "run_capture",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="2026-10-02T06:42:19-04:00\n"),
    )
    assert commands._provider_build_stamp(_provider(), Path(".")) == {
        "FAMILY_LIBRARIAN_P_BUILD_REVISION": CHECKOUT,
        "FAMILY_LIBRARIAN_P_BUILD_DATE": "2026-10-02T06:42:19-04:00",
    }


def test_no_stamp_without_a_conventional_source_dir_env_or_a_commit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(commands.lab_common, "git_commit", lambda _path, short=False: CHECKOUT)
    assert commands._provider_build_stamp(_provider(source_dir_env=None), Path(".")) == {}
    assert commands._provider_build_stamp(_provider(source_dir_env="SOMETHING_ELSE"), Path(".")) == {}
    monkeypatch.setattr(commands.lab_common, "git_commit", lambda _path, short=False: None)
    assert commands._provider_build_stamp(_provider(), Path(".")) == {}


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        (CHECKOUT, "OK"),
        ("7eb3e9d96b2c2753c6435902e0f496203666e791", "STALE"),
        ("", "no revision label"),
        (None, "not running"),
    ],
)
def test_report_flags_an_image_not_built_from_the_checkout(
    monkeypatch: pytest.MonkeyPatch, running: str | None, expected: str
):
    monkeypatch.setattr(
        commands.lab_common, "git_commit", lambda _path, short=False: CHECKOUT[:7] if short else CHECKOUT
    )
    monkeypatch.setattr(commands.lab_common, "repo_dir", lambda key=None: Path("."))
    monkeypatch.setattr(commands, "_running_image_revision", lambda _project, _service: running)
    lines = commands._build_revision_lines("proj", [_provider()])
    assert lines[0] == "Build revisions:"
    assert "family-librarian" in lines[1]
    assert expected in lines[2]
