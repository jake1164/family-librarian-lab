"""`./lab up` and `./lab clean-books` turn on the built-in sources a manually tested lab expects."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import commands  # noqa: E402


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status


class _Api:
    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, bool]] = []
        self._failing = failing

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> _Response:
        self.calls.append((provider_id, enabled))
        return _Response(500 if provider_id in self._failing else 200)


def test_gutenberg_and_librivox_are_on_by_default() -> None:
    assert commands.lab_builtin_sources({}) == ["gutendex", "librivox"]


def test_the_list_can_be_changed_or_emptied() -> None:
    assert commands.lab_builtin_sources({"FAMILY_LIBRARIAN_LAB_BUILTIN_SOURCES": " Gutendex , "}) == ["gutendex"]
    assert commands.lab_builtin_sources({"FAMILY_LIBRARIAN_LAB_BUILTIN_SOURCES": ""}) == []


def test_each_source_is_enabled_and_one_failure_does_not_stop_the_rest(capsys: pytest.CaptureFixture[str]) -> None:
    api = _Api(failing=("gutendex",))
    commands._enable_builtin_sources(api, {})  # type: ignore[arg-type]
    assert api.calls == [("gutendex", True), ("librivox", True)]
    captured = capsys.readouterr()
    assert "WARNING" in captured.err and "gutendex" in captured.err
    assert "librivox" in captured.out


def test_scenarios_still_keep_librivox_off() -> None:
    """The hermetic `./lab run` path is separate and must not change."""
    assert commands._LIVE_PROVIDERS_OFF_BY_DEFAULT == ("librivox",)
