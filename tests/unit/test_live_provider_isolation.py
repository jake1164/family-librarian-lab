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
