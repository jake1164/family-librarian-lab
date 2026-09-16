"""Unit coverage for the bot/household registration fallback `./lab up`'s
matrix wiring relies on when a previous run's container was never torn down
via `./lab base down` -- see _register_or_login_matrix_user's own docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from agent.simulators.matrix import MatrixApiError  # noqa: E402

from family_librarian_lab import commands  # noqa: E402


class _FakeMatrixClient:
    def __init__(self, *, register_error: MatrixApiError | None = None):
        self._register_error = register_error
        self.registered_with_token: str | None = "unset"
        self.logged_in = False

    def register_user(self, username: str, password: str, *, registration_token: str | None):
        self.registered_with_token = registration_token
        if self._register_error is not None:
            raise self._register_error
        return f"@{username}:matrix.example.test", "registered-token"

    def login(self, username: str, password: str):
        self.logged_in = True
        return f"@{username}:matrix.example.test", "login-token"


def test_register_or_login_returns_registration_result_when_registration_succeeds():
    client = _FakeMatrixClient()
    user_id, token = commands._register_or_login_matrix_user(
        client, "fl-bot", "fl-bot-password", registration_token="bootstrap-token"
    )
    assert (user_id, token) == ("@fl-bot:matrix.example.test", "registered-token")
    assert client.registered_with_token == "bootstrap-token"
    assert not client.logged_in


@pytest.mark.parametrize("errcode", ["M_FORBIDDEN", "M_USER_IN_USE"])
def test_register_or_login_falls_back_to_login_on_reused_container(errcode: str):
    client = _FakeMatrixClient(register_error=MatrixApiError(401, errcode, "already handled"))
    user_id, token = commands._register_or_login_matrix_user(
        client, "fl-bot", "fl-bot-password", registration_token="stale-bootstrap-token"
    )
    assert (user_id, token) == ("@fl-bot:matrix.example.test", "login-token")
    assert client.logged_in


def test_register_or_login_reraises_unrelated_matrix_errors():
    client = _FakeMatrixClient(register_error=MatrixApiError(500, "M_UNKNOWN", "homeserver is unwell"))
    with pytest.raises(MatrixApiError):
        commands._register_or_login_matrix_user(client, "fl-bot", "fl-bot-password", registration_token="token")
    assert not client.logged_in
