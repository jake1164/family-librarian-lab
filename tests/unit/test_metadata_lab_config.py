"""Credential lookup: environment wins, blanks are "not configured", no leaks."""

from __future__ import annotations

from pathlib import Path

import pytest

from metadata_lab import config
from metadata_lab.providers.base import ProviderAdapter


class _Keyless(ProviderAdapter):
    id = "keyless"


class _Keyed(ProviderAdapter):
    id = "keyed"
    credential_env = "MDLAB_TEST_TOKEN"


@pytest.fixture()
def lab_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "lab.env"
    monkeypatch.setattr(config, "lab_env_path", lambda: path)
    return path


def test_environment_wins_over_lab_env(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    lab_env.write_text("MDLAB_TEST_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MDLAB_TEST_TOKEN", "from-env")

    assert config.get_credential("MDLAB_TEST_TOKEN") == "from-env"


def test_lab_env_used_when_environment_is_empty(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MDLAB_TEST_TOKEN", raising=False)
    lab_env.write_text("MDLAB_TEST_TOKEN=from-file\n", encoding="utf-8")

    assert config.get_credential("MDLAB_TEST_TOKEN") == "from-file"


def test_blank_value_is_not_configured(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MDLAB_TEST_TOKEN", raising=False)
    lab_env.write_text("MDLAB_TEST_TOKEN=\nMDLAB_TEST_OTHER=''\n", encoding="utf-8")

    assert config.get_credential("MDLAB_TEST_TOKEN") is None
    assert config.get_credential("MDLAB_TEST_OTHER") is None


def test_comments_quotes_and_blank_lines_are_handled(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MDLAB_TEST_TOKEN", raising=False)
    lab_env.write_text(
        "# a comment\n"
        "\n"
        "exported_garbage_line_without_equals\n"
        'MDLAB_TEST_TOKEN="quoted value"\n'
        "MDLAB_TEST_SECOND='single quoted'\n",
        encoding="utf-8",
    )

    assert config.get_credential("MDLAB_TEST_TOKEN") == "quoted value"
    assert config.get_credential("MDLAB_TEST_SECOND") == "single quoted"


def test_alias_names_are_tried_in_order(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MDLAB_TEST_TOKEN", raising=False)
    monkeypatch.setenv("MDLAB_TEST_ALIAS", "alias-value")

    assert config.get_credential("MDLAB_TEST_TOKEN", "MDLAB_TEST_ALIAS") == "alias-value"


def test_missing_lab_env_file_is_not_an_error(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MDLAB_TEST_TOKEN", raising=False)

    assert config.get_credential("MDLAB_TEST_TOKEN") is None


def test_is_configured_distinguishes_keyless_and_keyed(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MDLAB_TEST_TOKEN", raising=False)

    assert config.is_configured(_Keyless()) is True
    assert config.is_configured(_Keyed()) is False
    monkeypatch.setenv("MDLAB_TEST_TOKEN", "value")
    assert config.is_configured(_Keyed()) is True


def test_credential_value_never_appears_in_object_repr(lab_env: Path, monkeypatch: pytest.MonkeyPatch):
    from metadata_lab.http import HttpClient, TransportResponse

    class _Transport:
        def send(self, method, url, *, params, headers, body, timeout):
            return TransportResponse(200, {}, "{}")

    secret = "SECRET-12345"
    monkeypatch.setenv("MDLAB_TEST_TOKEN", secret)
    client = HttpClient(provider_id="keyed", transport=_Transport(), min_interval_s=0.0)
    ctx = client.context(credential=config.get_credential("MDLAB_TEST_TOKEN"))

    assert secret not in repr(ctx)
    assert secret not in repr(_Keyed())
    assert "set" in repr(ctx)
