"""Unit coverage for the human-facing part of ``lab status``."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import clients  # noqa: E402
from family_librarian_lab import commands  # noqa: E402


@pytest.fixture(autouse=True)
def _disable_terminal_color(monkeypatch: pytest.MonkeyPatch):
    """Keep output assertions stable with either supported se-lab revision."""
    monkeypatch.setattr(commands.lab_common, "colorize_urls", lambda text: text, raising=False)


def test_status_prints_running_service_connection_details(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    values = {
        "FAMILY_LIBRARIAN_ADMIN_EMAIL": "admin@example.test",
        "FAMILY_LIBRARIAN_ADMIN_PASSWORD": "lab-password",
    }
    services = {
        "family-librarian": {"state": "running"},
        clients.CWA_SERVICE: {"state": "running"},
        clients.ABS_SERVICE: {"state": "running"},
        clients.CWA_SFTP_SERVICE_KEY: {"state": "running"},
        clients.SMTP_SERVICE: {"state": "exited"},
    }
    monkeypatch.setenv("LAB_EXTERNAL_HOST", "toontown-int-srv2")
    monkeypatch.setattr(commands, "_load_lab_env", lambda: values)
    monkeypatch.setattr(commands, "_project_name", lambda *_args, **_kwargs: "family-librarian-lab")
    monkeypatch.setattr(
        commands,
        "_compose",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="[]\n", stderr="", returncode=0),
    )
    monkeypatch.setattr(commands, "_readiness", lambda *_args: ({"compose_services": services}, True))

    assert commands.handle_status(SimpleNamespace(project_name=None), config=None) == 0

    output = capsys.readouterr().out
    assert "Connection info:" in output
    assert "Family Librarian: http://toontown-int-srv2:18080" in output
    assert "user: admin@example.test / password: lab-password" in output
    assert "CWA: http://toontown-int-srv2:18083" in output
    assert "OPDS user: admin / password: admin123" in output
    assert "Audiobookshelf: http://toontown-int-srv2:18378" in output
    assert f"user: {clients.ABS_DEFAULT_USERNAME} / password: {clients.ABS_DEFAULT_PASSWORD}" in output
    assert "CWA ingest transport: SFTP" in output
    assert "Mailpit (SMTP)" not in output


def test_status_hides_connection_details_when_app_is_not_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(
        commands,
        "_compose",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="[]\n", stderr="", returncode=0),
    )
    monkeypatch.setattr(commands, "_load_lab_env", lambda: {})
    monkeypatch.setattr(commands, "_project_name", lambda *_args, **_kwargs: "family-librarian-lab")
    monkeypatch.setattr(commands, "_readiness", lambda *_args: ({"compose_services": {}}, False))

    assert commands.handle_status(SimpleNamespace(project_name=None), config=None) == 1
    assert "Connection info:" not in capsys.readouterr().out
