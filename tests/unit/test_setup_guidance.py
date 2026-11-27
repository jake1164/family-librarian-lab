"""Setup guidance presets: what `./lab up` sends, and that an older product build without the routes still comes up."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import commands  # noqa: E402
from family_librarian_lab.api import ApiResponse, FamilyLibrarianApi  # noqa: E402


class _RecordingApi(FamilyLibrarianApi):
    def __init__(self, status: int = 200, body: object | None = None):
        super().__init__("http://lab.invalid")
        self.calls: list[tuple[str, str, object]] = []
        self._status = status
        self._body = body if body is not None else {}

    def _request(self, method, path, *, json_body=None, data=None, headers=None, timeout=30):
        self.calls.append((method, path, json_body))
        return ApiResponse(status=self._status, body=self._body)


def test_kindle_and_messaging_guidance_use_the_admin_routes():
    api = _RecordingApi()

    assert api.set_kindle_guidance(sender_address="cwa@example.test", amazon_store=None)
    assert api.set_messaging_guidance(sign_in_instructions="Sign in.", web_app_url="http://lab:18009")
    assert api.set_show_element_help(False)

    assert [(method, path) for method, path, _ in api.calls] == [
        ("PUT", "/api/v1/admin/setup-guidance/kindle"),
        ("PUT", "/api/v1/admin/setup-guidance/messaging"),
        ("PUT", "/api/v1/admin/setup-guidance/messaging/element-help"),
    ]
    assert api.calls[0][2] == {"kindleSenderAddress": "cwa@example.test", "amazonStore": None}
    assert api.calls[1][2]["serverName"] is None
    assert api.calls[2][2] == {"enabled": False}


@pytest.mark.parametrize("status", [404, 405])
def test_a_build_without_setup_guidance_is_reported_not_failed(status: int):
    api = _RecordingApi(status=status)

    assert api.set_kindle_guidance(sender_address="cwa@example.test") is False
    assert api.set_messaging_guidance() is False


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_a_rejected_preset_is_a_failure(status: int):
    with pytest.raises(AssertionError):
        _RecordingApi(status=status).set_kindle_guidance(sender_address="cwa@example.test")


def test_a_skipped_preset_says_so(capsys):
    commands._preset_setup_guidance(True, "Kindle")
    assert capsys.readouterr().out == ""

    commands._preset_setup_guidance(False, "Kindle")
    assert "no Kindle setup guidance" in capsys.readouterr().out


@pytest.mark.parametrize("matrix_running", [True, False])
def test_up_prints_the_first_time_setup_member(monkeypatch, matrix_running: bool):
    printed = []
    monkeypatch.setattr(commands.lab_common, "print_connection_info", printed.extend)

    commands._print_connection_info(
        {"FAMILY_LIBRARIAN_ADMIN_EMAIL": "admin@example.test", "FAMILY_LIBRARIAN_ADMIN_PASSWORD": "fixture-only"},
        cwa_running=True,
        matrix_running=matrix_running,
    )

    entry = next(item for item in printed if item.name == "First-time setup test member")
    assert commands.clients.NEWCOMER_EMAIL in entry.credentials
    assert (commands.clients.MATRIX_NEWCOMER_USERNAME + " /" in entry.credentials) is matrix_running


def test_a_catcher_relay_scenario_never_sees_the_real_relay_settings():
    values = {
        "FAMILY_LIBRARIAN_CWA_SMTP_HOST": "relay.example.test",
        "FAMILY_LIBRARIAN_CWA_SMTP_PASSWORD": "fixture-only",
        "FAMILY_LIBRARIAN_CWA_SMTP_FROM": "real-sender@example.test",
        "FAMILY_LIBRARIAN_HOST_PORT": "18080",
    }

    kept = commands._without_real_cwa_relay(values)

    assert kept == {"FAMILY_LIBRARIAN_HOST_PORT": "18080"}
    relay = commands._cwa_relay_settings(kept)
    assert not relay.is_real
    assert relay.host == commands.clients.CWA_MAILPIT_INTERNAL_HOST
    assert relay.from_address != values["FAMILY_LIBRARIAN_CWA_SMTP_FROM"]
    assert commands._cwa_relay_settings(values).is_real


@pytest.mark.parametrize("catcher_relay", [False, True])
def test_the_factory_applies_the_catcher_only_when_a_case_asks(monkeypatch, catcher_relay: bool):
    built = []
    monkeypatch.setattr(commands, "_BaseScenario", lambda values, test_id, **_options: built.append(values))
    factory = commands._BaseScenarioFactory({"FAMILY_LIBRARIAN_CWA_SMTP_HOST": "relay.example.test"}, keep=False)

    factory("KIN-03", catcher_relay=catcher_relay)

    assert ("FAMILY_LIBRARIAN_CWA_SMTP_HOST" in built[0]) is not catcher_relay


@pytest.mark.parametrize(
    "page, expected",
    [
        ('<input type="text" class="x" name="mail_server" value="cwa-mailpit">', "cwa-mailpit"),
        ("<input value='a&amp;b' id=\"y\" name='mail_server'/>", "a&b"),
        ('<input name="mail_server_type" value="0"><input name="mail_server" value="">', ""),
        ('<input name="mail_server">', None),
        ('<input name="other" value="cwa-mailpit">', None),
    ],
)
def test_the_saved_relay_is_read_from_the_settings_form(page: str, expected: str | None):
    assert commands.clients._input_value(page, "mail_server") == expected
