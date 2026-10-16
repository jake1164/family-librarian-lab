"""Every lab client must be constructible the way the scenario wiring builds it.

An earlier edit inserted a new class between `@dataclass(slots=True)` and `class CwaClient:`, which silently moved the
decorator onto the new class: `CwaClient(host_base_url=...)` then raised "CwaClient() takes no arguments" and every suite
that brings up CWA failed (61 of 73 cases in one run). Nothing here constructed a client, so the unit tests passed.
"""

from __future__ import annotations

import dataclasses

from family_librarian_lab import clients

BASE = "http://127.0.0.1:1"


def test_cwa_client_takes_its_host_base_url():
    assert clients.CwaClient(host_base_url=BASE).host_base_url == BASE


def test_abs_client_takes_its_host_base_url():
    assert clients.AbsClient(host_base_url=BASE).host_base_url == BASE


def test_the_dataclass_clients_are_still_dataclasses():
    for name in ("CwaClient", "AbsClient", "CwaAdminSession"):
        assert dataclasses.is_dataclass(getattr(clients, name)), f"{name} lost its @dataclass decorator"


def test_sample_provider_control_is_a_plain_class_with_its_own_init():
    control = clients.SampleProviderControl(BASE + "/")
    assert control.host_base_url == BASE
    assert not dataclasses.is_dataclass(clients.SampleProviderControl)
