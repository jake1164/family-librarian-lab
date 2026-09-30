"""Run-scoped shared services: one Postgres (per-scenario databases) and one
ClamAV for a whole `lab run`, instead of rebuilding them per case or suite.

These pin the decisions that keep isolation intact: each scenario gets its own
database, suites that need a real server can opt out, readiness does not
expect services a shared-database scenario never starts, and a suite teardown
cannot pull ClamAV out from under the rest of the run.
"""

from __future__ import annotations

import pytest

from family_librarian_lab import commands

_SHARED_ENV = {
    "FAMILY_LIBRARIAN_LOCAL_DB_PROFILE": "local-db",
    "FAMILY_LIBRARIAN_DB_HOST": "172.17.0.1",
    "FAMILY_LIBRARIAN_DB_PORT": "15432",
}


def test_database_names_are_unique_valid_identifiers() -> None:
    project = "family-librarian-lab-cwa-s-02-password-20260930005717111285"

    name = commands._scenario_database_name(project)

    assert name == "fl_cwa_s_02_password_20260930005717111285"
    assert len(name) <= 63


def test_an_unsafe_project_name_is_refused_rather_than_quoted_into_sql() -> None:
    with pytest.raises(ValueError):
        commands._scenario_database_name('family-librarian-lab-x"; DROP DATABASE fl_template; --')


def test_a_shared_database_scenario_gets_its_own_database_name() -> None:
    first = commands._BaseScenario({}, "CWA-L-01", keep=False, shared_database_env=_SHARED_ENV)
    second = commands._BaseScenario({}, "CWA-L-01", keep=False, shared_database_env=_SHARED_ENV)

    assert first._values["FAMILY_LIBRARIAN_DB_NAME"] != second._values["FAMILY_LIBRARIAN_DB_NAME"]
    assert first._values["FAMILY_LIBRARIAN_LOCAL_DB_PROFILE"] == "local-db"


def test_the_factory_honours_a_suite_opting_out_of_the_shared_database() -> None:
    factory = commands._BaseScenarioFactory({}, keep=False)
    factory.shared_database_env = _SHARED_ENV

    assert factory("CWA-L-01")._database is not None
    factory.local_database = True
    local = factory("BACKUP-01")
    assert local._database is None
    assert "FAMILY_LIBRARIAN_DB_NAME" not in local._values


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def _ps(*services: tuple[str, str, str | None, int]) -> _Result:
    import json

    return _Result("\n".join(
        json.dumps({"Service": s, "State": st, "Health": h, "ExitCode": code}) for s, st, h, code in services
    ))


def test_readiness_does_not_expect_postgres_or_migrate_with_a_shared_database(monkeypatch) -> None:
    monkeypatch.setattr(commands, "_compose", lambda *a, **k: _ps(("family-librarian", "running", "healthy", 0)))

    _, shared_ok = commands._compose_service_health(_SHARED_ENV, "p")
    _, local_ok = commands._compose_service_health({}, "p")

    assert shared_ok is True
    # Without the shared database, a project missing postgres/migrate is broken.
    assert local_ok is False


def test_a_suite_teardown_leaves_clamav_up_while_the_run_owns_it(monkeypatch) -> None:
    stopped: list[bool] = []
    monkeypatch.setattr(commands, "_stop_shared_clamav", lambda: stopped.append(True))

    monkeypatch.setattr(commands, "_RUN_OWNS_SHARED_SERVICES", True)
    commands.teardown_shared_clamav()
    assert stopped == []

    monkeypatch.setattr(commands, "_RUN_OWNS_SHARED_SERVICES", False)
    commands.teardown_shared_clamav()
    assert stopped == [True]
