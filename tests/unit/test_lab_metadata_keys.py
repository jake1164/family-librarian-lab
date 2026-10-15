"""`./lab up` and `./lab clean-books` turn on the metadata sources whose keys lab.env provides."""

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
    def __init__(self, credential_status: int = 200) -> None:
        self.calls: list[tuple] = []
        self._credential_status = credential_status

    def set_metadata_credential(self, provider_id: str, credential: str) -> _Response:
        self.calls.append(("credential", provider_id, credential))
        return _Response(self._credential_status)

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> _Response:
        self.calls.append(("enable", provider_id, enabled))
        return _Response(200)


def test_only_sources_with_a_key_are_configured() -> None:
    assert commands.lab_metadata_credentials({}) == []
    assert commands.lab_metadata_credentials({"FAMILY_LIBRARIAN_LAB_HARDCOVER_TOKEN": "  "}) == []
    assert commands.lab_metadata_credentials(
        {"FAMILY_LIBRARIAN_LAB_HARDCOVER_TOKEN": " tok ", "FAMILY_LIBRARIAN_LAB_GOOGLEBOOKS_KEY": "gb"}
    ) == [("hardcover", "tok"), ("googlebooks", "gb")]


def test_the_key_is_stored_before_the_source_is_enabled_and_never_printed(capsys: pytest.CaptureFixture[str]) -> None:
    api = _Api()
    commands._apply_lab_metadata(api, {"FAMILY_LIBRARIAN_LAB_HARDCOVER_TOKEN": "secret-token"})  # type: ignore[arg-type]
    assert api.calls == [("credential", "hardcover", "secret-token"), ("enable", "hardcover", True)]
    captured = capsys.readouterr()
    assert "secret-token" not in captured.out + captured.err


def test_a_source_whose_key_was_refused_is_not_enabled_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    api = _Api(credential_status=400)
    commands._apply_lab_metadata(api, {"FAMILY_LIBRARIAN_LAB_GOOGLEBOOKS_KEY": "bad"})  # type: ignore[arg-type]
    assert ("enable", "googlebooks", True) not in api.calls
    assert "WARNING" in capsys.readouterr().err


def test_nothing_happens_without_keys() -> None:
    api = _Api()
    commands._apply_lab_metadata(api, {})  # type: ignore[arg-type]
    assert api.calls == []
