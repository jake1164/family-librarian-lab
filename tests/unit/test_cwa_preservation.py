"""CWA's real form contracts: preserve unrelated settings and verify saved preservation flags."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "se-lab"))
from family_librarian_lab.clients import CwaAdminSession, CwaClient
from family_librarian_lab import clients, commands

BASIC = ("config_embed_metadata",)
INGEST = ("auto_convert", "auto_metadata_enforcement", "kindle_epub_fixer", "auto_metadata_fetch_enabled")


@contextmanager
def cwa_forms(*, reject=False, persist=True, missing=False, reboot=False, ingest_form_token=True):
    state = {"enabled": set(BASIC + INGEST), "posts": [], "reject": reject, "persist": persist, "missing": missing,
             "unready": 0, "restart_probes": 0}

    def page(path):
        flags = BASIC if path == "/admin/config" else INGEST
        token = "basic-csrf" if path == "/admin/config" else "ingest-csrf"
        controls = "".join(f'<input type="checkbox" name="{flag}" {"checked" if flag in state["enabled"] else ""}>'
                           for flag in flags if not (missing and flag == BASIC[0]))
        csrf = f'<input name="csrf_token" value="{token}">' if path == "/admin/config" or ingest_form_token else ""
        return (f'<form>{csrf}{controls}'
                '<input type="checkbox" name="unrelated_checked" value="on" checked>'
                '<input type="checkbox" name="unrelated_unchecked" value="on">'
                '<input type="text" name="unchanged" value="A &amp; B">'
                '<input name="disabled_control" value="not-submitted" disabled>'
                '<select name="selected"><option value="wrong">Wrong</option><option value="right" selected>Right</option></select>'
                '<select name="first_option"><option value="first">First</option><option value="second">Second</option></select>'
                '<select name="multi" multiple><option value="a" selected>A</option><option value="b" selected>B</option></select>'
                '<textarea name="json_setting">{&quot;provider&quot;: &quot;example&quot;}</textarea>'
                '<input type="submit" name="submit_button" value="Submit">'
                '<input type="submit" name="submit_button" value="Apply Default Settings"></form>')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body, status=200, cookie=False):
            raw = body.encode()
            self.send_response(status)
            if cookie:
                self.send_header("Set-Cookie", "session=fixture; Path=/")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/admin/config" and state["unready"]:
                state["unready"] -= 1
                state["restart_probes"] += 1
                self.send("Restarting", status=503)
                return
            if self.path.startswith("/opds/download/"):
                state["download_auth"] = self.headers.get("Authorization")
                self.send("fictional EPUB bytes", status=503 if reject else 200)
            elif self.path == "/login" or self.headers.get("Cookie") != "session=fixture":
                self.send('<form><input name="csrf_token" value="login-csrf"><input name="password"></form>')
            else:
                self.send(page(self.path))

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode(), keep_blank_values=True)
            if self.path == "/login":
                self.send('<input name="csrf_token" value="session-csrf">', cookie=True)
                return
            state["posts"].append((self.path, fields))
            basic = self.path == "/admin/ajaxconfig"
            assert fields["csrf_token"] == ["basic-csrf" if basic else "ingest-csrf" if ingest_form_token else "session-csrf"]
            assert fields["unrelated_checked"] == ["on"] and "unrelated_unchecked" not in fields
            assert fields["unchanged"] == ["A & B"] and "disabled_control" not in fields
            assert fields["selected"] == ["right"] and fields["first_option"] == ["first"]
            assert fields["multi"] == ["a", "b"]
            assert fields["json_setting"] == ['{"provider": "example"}']
            if not basic:
                assert fields["submit_button"] == ["Submit"], "Never submit Apply Default Settings."
            if reject:
                self.send(json.dumps({"result": [{"type": "danger", "message": "rejected"}]}))
                return
            if persist:
                for flag in BASIC if basic else INGEST:
                    if flag not in fields:
                        state["enabled"].discard(flag)
            if basic:
                state["unready"] = 2 if reboot else 0
                self.send(json.dumps({"result": [{"type": "success"}], "reboot": reboot}))
            else:
                self.send(page("/cwa-settings"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_configuration_disables_both_rewriters_and_preserves_other_form_values():
    with cwa_forms() as (url, state):
        client = CwaAdminSession(url)
        client.configure_preserved_artifacts()
        assert state["enabled"] == set()
        assert [path for path, _ in state["posts"]] == ["/admin/ajaxconfig", "/cwa-settings"]
        client.configure_preserved_artifacts()
        assert len(state["posts"]) == 2, "Already-preserving settings need no second POST."


def test_http_200_validation_error_does_not_count_as_a_saved_configuration():
    with cwa_forms(reject=True) as (url, state):
        with pytest.raises(AssertionError, match="rejected"):
            CwaAdminSession(url).configure_preserved_artifacts()
        assert len(state["posts"]) == 1


def test_readback_must_confirm_the_setting_was_actually_saved():
    with cwa_forms(persist=False) as (url, _state):
        with pytest.raises(AssertionError, match="still enabled"):
            CwaAdminSession(url).configure_preserved_artifacts()


def test_unrecognized_form_fails_before_any_configuration_post():
    with cwa_forms(missing=True) as (url, state):
        with pytest.raises(AssertionError, match="config_embed_metadata"):
            CwaAdminSession(url).configure_preserved_artifacts()
        assert state["posts"] == []


def test_a_successful_basic_config_save_can_restart_cwa_before_readback():
    with cwa_forms(reboot=True) as (url, state):
        CwaAdminSession(url).configure_preserved_artifacts()
        assert state["enabled"] == set()
        assert state["restart_probes"] == 2


def test_cwa_settings_without_an_embedded_token_uses_the_authenticated_session_token():
    with cwa_forms(ingest_form_token=False) as (url, state):
        CwaAdminSession(url).configure_preserved_artifacts()
        assert state["enabled"] == set()


def test_download_reads_actual_opds_bytes_with_the_lab_account():
    with cwa_forms() as (url, state):
        assert CwaClient(url).download_epub("42") == b"fictional EPUB bytes"
        assert state["download_auth"].startswith("Basic ")


def test_unavailable_download_is_a_failure_rather_than_book_content():
    with cwa_forms(reject=True) as (url, _state):
        with pytest.raises(AssertionError, match="503"):
            CwaClient(url).download_epub("42")


@pytest.mark.parametrize("profile", [clients.CWA_PROFILE, clients.CWA_SFTP_PROFILE_PASSWORD])
def test_both_ingest_transports_require_preservation_before_fl_is_wired(monkeypatch, profile):
    events = []

    class Admin:
        def __init__(self, **_kwargs):
            pass

        def configure_preserved_artifacts(self):
            events.append("preserve")

        def configure_mail_settings(self, **_kwargs):
            pass

        def ensure_ereader_service_account(self, **_kwargs):
            pass

    class Api:
        def configure_cwa_local(self, **_kwargs):
            assert events == ["preserve"]
            events.append("wire-local")

        def configure_cwa_sftp(self, **_kwargs):
            assert events == ["preserve"]
            events.append("wire-sftp")

    monkeypatch.setattr(clients, "CwaAdminSession", Admin)
    commands._wire_destinations({"FAMILY_LIBRARIAN_SFTP_PASSWORD": "fixture-only"}, "fixture-only", (profile,), Api(), seed_readers=False)
    assert events == ["preserve", "wire-local" if profile == clients.CWA_PROFILE else "wire-sftp"]
