"""Scenarios must not depend on a public third-party API.

CWA-L-11/CWA-L-12 failed with a bare socket timeout on 2026-09-30 because FL's
fulfillment-options call waited on librivox.org, which no case exercises and
which took 10-15 s per request from the lab host. These tests pin the guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from family_librarian_lab import commands


@dataclass
class _Response:
    status: int


@dataclass
class _FakeApi:
    status: int = 200
    calls: list[tuple[str, bool]] = field(default_factory=list)

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> _Response:
        self.calls.append((provider_id, enabled))
        return _Response(self.status)


def test_librivox_is_listed_as_a_live_provider_to_disable() -> None:
    assert "librivox" in commands._LIVE_PROVIDERS_OFF_BY_DEFAULT


def test_every_listed_provider_is_disabled_not_enabled() -> None:
    api = _FakeApi()

    commands._disable_live_providers(api)  # type: ignore[arg-type]

    assert api.calls == [(p, False) for p in commands._LIVE_PROVIDERS_OFF_BY_DEFAULT]


def test_a_failed_disable_fails_the_scenario_instead_of_going_live_silently() -> None:
    with pytest.raises(AssertionError, match="hermetic"):
        commands._disable_live_providers(_FakeApi(status=404))  # type: ignore[arg-type]


@pytest.mark.parametrize("manual_value", [None, "false", "true"])
def test_run_enables_demo_without_changing_manual_settings(manual_value, monkeypatch) -> None:
    monkeypatch.delenv("FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED", raising=False)
    values = {} if manual_value is None else {"FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED": manual_value}
    original = values.copy()
    factory = commands._BaseScenarioFactory(values, keep=False)
    # Suites replace extra_env during setup/teardown; the fixture must survive that.
    factory.extra_env = {"SAMPLE_PROVIDER_QUESTION_FIXTURES": "1"}
    scenario = factory("SEC-01")
    environments = []

    def run(command, **kwargs):
        environments.append(kwargs["env"])
        return None

    monkeypatch.setattr(commands.subprocess, "run", run)
    commands._compose(scenario._values, scenario.project_name, "config")
    commands._compose(values, "manual-up", "config")

    assert environments[0]["FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED"] == "true"
    assert environments[0]["SAMPLE_PROVIDER_QUESTION_FIXTURES"] == "1"
    assert values == original
    assert factory.extra_env == {"SAMPLE_PROVIDER_QUESTION_FIXTURES": "1"}
    if manual_value is None:
        assert "FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED" not in environments[1]
    else:
        assert environments[1]["FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED"] == manual_value
