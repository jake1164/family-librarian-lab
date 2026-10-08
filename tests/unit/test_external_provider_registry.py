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


def test_alias_resolves_to_canonical_provider_with_one_warning(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(
        commands.lab_common, "load_local_registry",
        lambda _name: {"providers": [_entry(name="new-name", aliases=["old-name"])]},
    )
    monkeypatch.setattr(commands, "_WARNED_ALIASES", set())
    assert commands._require_provider("old-name").name == "new-name"
    assert commands._require_provider("old-name").name == "new-name"
    assert commands._require_provider("new-name").name == "new-name"
    err = capsys.readouterr().err
    assert err.count("deprecated") == 1
    assert "'new-name'" in err
    assert commands._provider_selector_names() == ["new-name", "old-name"]


def test_unknown_provider_still_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(commands.lab_common, "load_local_registry", lambda _name: {"providers": [_entry()]})
    with pytest.raises(SystemExit):
        commands._require_provider("nope")


class _FakeDocker:
    """Records docker calls; volumes maps name -> {"empty": bool, "marker": bool}."""

    def __init__(self, volumes, users=()):
        self.volumes, self.users, self.calls = volumes, list(users), []

    def __call__(self, *args):
        self.calls.append(args)
        out = SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ("volume", "inspect"):
            out.returncode = 0 if args[2] in self.volumes else 1
        elif args[:2] == ("volume", "create"):
            self.volumes[args[-1]] = {"empty": True, "marker": False}
        elif args[0] == "ps":
            out.stdout = "\n".join(self.users)
        elif args[0] == "run":
            vol = next(a.split(":")[0] for a in args if a.endswith((":/v:ro", ":/to")))
            if args[-3:] == ("test", "-f", args[-1]):
                out.returncode = 0 if self.volumes[vol]["marker"] else 1
            elif args[-2] == "touch":
                self.volumes[vol]["marker"] = True
            elif args[-1] == "ls -A /v":
                out.stdout = "" if self.volumes[vol]["empty"] else "x\n"
            elif "cp -a" in args[-1]:
                self.volumes[vol]["empty"] = False
            else:
                out.stdout = "same\n"
        return out


def _migrating_provider():
    return commands.ProviderConfig(
        name="p", compose_file="x", internal_url="http://p", app_service="p-app",
        volume_migrations=[{"from": "old-data", "to": "new-data"}],
    )


def test_volume_migration_copies_verifies_marks_and_is_then_a_noop(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeDocker({"proj_old-data": {"empty": False, "marker": False}}, users=["c1"])
    monkeypatch.setattr(commands, "_docker", fake)
    commands._migrate_provider_volumes(_migrating_provider(), "proj")
    assert fake.volumes["proj_new-data"]["marker"] is True
    assert any(c[0] == "stop" for c in fake.calls)
    assert not any(c[:2] == ("volume", "rm") for c in fake.calls)
    fake.calls.clear()
    commands._migrate_provider_volumes(_migrating_provider(), "proj")
    assert not any("cp -a" in str(c) for c in fake.calls)


def test_volume_migration_refuses_unmarked_populated_target(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeDocker({"proj_old-data": {"empty": False, "marker": False}, "proj_new-data": {"empty": False, "marker": False}})
    monkeypatch.setattr(commands, "_docker", fake)
    with pytest.raises(SystemExit):
        commands._migrate_provider_volumes(_migrating_provider(), "proj")
    assert not any("cp -a" in str(c) for c in fake.calls)


def test_volume_migration_skips_when_old_volume_absent(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeDocker({})
    monkeypatch.setattr(commands, "_docker", fake)
    commands._migrate_provider_volumes(_migrating_provider(), "proj")
    assert not any(c[0] in ("stop", "run") for c in fake.calls)
