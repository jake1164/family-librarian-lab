"""Element Web (part of the matrix profile) is configured from a generated file: the homeserver address must be the one
a browser reaches, so it is written per host rather than tracked."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import clients  # noqa: E402


def test_config_points_the_browser_at_the_given_homeserver_and_locks_the_client(tmp_path: Path):
    path = clients.ensure_element_config(tmp_path / "element", "http://lab-host:18008")

    config = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "config.json"
    assert config["default_server_config"]["m.homeserver"] == {
        "base_url": "http://lab-host:18008",
        "server_name": clients.MATRIX_SERVER_NAME,
    }
    assert config["disable_custom_urls"] is True
    assert config["disable_guests"] is True
    assert config["disable_3pid_login"] is True


def test_config_follows_the_host_and_is_not_rewritten_when_unchanged(tmp_path: Path):
    directory = tmp_path / "element"
    path = clients.ensure_element_config(directory, "http://127.0.0.1:18008")
    first_mtime = path.stat().st_mtime_ns

    clients.ensure_element_config(directory, "http://127.0.0.1:18008")
    assert path.stat().st_mtime_ns == first_mtime

    clients.ensure_element_config(directory, "http://toontown-int-srv2:18008")
    assert json.loads(path.read_text(encoding="utf-8"))["default_server_config"]["m.homeserver"]["base_url"] == (
        "http://toontown-int-srv2:18008"
    )


def test_compose_runs_element_only_in_the_matrix_profile_with_a_pinned_image():
    compose = (REPO_ROOT / "compose.base.yaml").read_text(encoding="utf-8")
    start = compose.index("\n  element:\n")
    block = compose[start : compose.index("\nvolumes:", start)]

    assert "profiles: [matrix]" in block
    assert f"{clients.ELEMENT_DEFAULT_IMAGE}}}" in block, "the default image must match clients.ELEMENT_DEFAULT_IMAGE"
    assert ":latest" not in block
    assert ":/app/config.json:ro" in block
