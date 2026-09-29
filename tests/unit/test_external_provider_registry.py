"""Unit coverage for external-provider registry entries with companion services."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from family_librarian_lab import commands  # noqa: E402


def _entry(**extra):
    return {
        "name": "p", "compose_file": "external-providers/p.local.yaml",
        "internal_url": "http://p-vpn:9000", "app_service": "p-app", **extra,
    }


def test_registry_reads_companion_services(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        commands.lab_common, "load_local_registry",
        lambda _name: {"providers": [_entry(vpn_service="p-vpn", companion_services=["p-dl", "p-idx"]), _entry(name="q")]},
    )
    providers = commands._load_provider_registry()
    assert providers["p"].companion_services == ["p-dl", "p-idx"]
    assert providers["q"].companion_services is None


def test_restart_orders_sidecar_then_companions_then_app(monkeypatch: pytest.MonkeyPatch):
    provider = commands.ProviderConfig(
        name="p", compose_file="external-providers/p.local.yaml", internal_url="http://p-vpn:9000",
        app_service="p-app", vpn_service="p-vpn", companion_services=["p-dl", "p-idx"],
    )
    restarted: list[str] = []
    monkeypatch.setattr(commands, "_require_provider", lambda _name: provider)
    monkeypatch.setattr(commands, "_load_lab_env", lambda: {})
    monkeypatch.setattr(commands.lab_common, "project_name", lambda: "proj")
    monkeypatch.setattr(commands, "_run_or_exit", lambda _v, _p, _verb, service, **_kw: restarted.append(service))
    assert commands.handle_restart_external_provider(SimpleNamespace(ep="p"), None) == 0
    assert restarted == ["p-vpn", "p-dl", "p-idx", "p-app"]


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        ({"p-app", "p-dl"}, "running"),
        ({"p-app"}, "degraded (not running: p-dl)"),
        ({"p-dl"}, "not running"),
    ],
)
def test_provider_state_reflects_companions(running: set[str], expected: str):
    provider = commands.ProviderConfig(
        name="p", compose_file="x", internal_url="http://p", app_service="p-app", companion_services=["p-dl"],
    )
    checks = {"compose_services": {name: {"state": "running"} for name in running}}
    assert commands._provider_state(checks, provider) == expected
