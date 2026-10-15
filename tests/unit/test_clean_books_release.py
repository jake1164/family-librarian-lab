"""`./lab clean-books` has every provider release its downloads before FL's records are wiped, and never fails because
that cleanup could not run."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import commands  # noqa: E402


class _Api:
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self._result = result
        self._error = error

    def release_provider_downloads(self) -> dict:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result or {}


def test_the_counts_are_reported(capsys: pytest.CaptureFixture[str]) -> None:
    api = _Api({"cancelled": 2, "released": 5, "failed": 1})
    commands._release_provider_downloads(api)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert api.calls == 1
    assert "2 cancelled" in out and "5 cleaned up" in out and "1 could not be reached" in out


def test_a_failed_release_warns_and_does_not_stop_the_reset(capsys: pytest.CaptureFixture[str]) -> None:
    commands._release_provider_downloads(_Api(error=AssertionError("HTTP 500")))  # type: ignore[arg-type]
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "retention" in captured.err


def test_the_help_says_providers_are_cleaned_and_qbittorrent_is_not() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    commands._configure_clean_books(parser)
    text = parser.description or ""
    assert "staged files" in text
    assert "qBittorrent is left" in text


@pytest.mark.parametrize("status", [404, 405])
def test_an_older_family_librarian_is_named_as_the_reason(status: int) -> None:
    from family_librarian_lab.api import ApiResponse, FamilyLibrarianApi

    api = FamilyLibrarianApi("http://127.0.0.1:1")
    api._request = lambda *args, **kwargs: ApiResponse(status, None)  # type: ignore[method-assign,assignment]

    with pytest.raises(AssertionError, match="predates this feature"):
        api.release_provider_downloads()
