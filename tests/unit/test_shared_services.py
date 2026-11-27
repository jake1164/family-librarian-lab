"""Run-scoped shared services: one Postgres (per-scenario databases) and one
ClamAV for a whole `lab run`, instead of rebuilding them per case or suite.

These pin the decisions that keep isolation intact: each scenario gets its own
database, suites that need a real server can opt out, readiness does not
expect services a shared-database scenario never starts, and a suite teardown
cannot pull ClamAV out from under the rest of the run.
"""

from __future__ import annotations

from types import SimpleNamespace

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


def test_teardown_profiles_cover_every_scenario_profile_in_compose() -> None:
    """`down` only removes services in an active profile. A profile missing
    from ALL_PROFILES leaks its containers and their host ports into every
    later case (EXTPROV-02..04 failed this way on 2026-09-30)."""
    import re

    text = commands.COMPOSE_FILE.read_text(encoding="utf-8")
    declared = set()
    for match in re.finditer(r"^\s+profiles:\s*\[([^\]]*)\]", text, re.MULTILINE):
        for item in match.group(1).split(","):
            name = item.strip().strip('"').strip("'")
            if name and not name.startswith("${"):
                declared.add(name)
    run_scoped = {commands.SHARED_CLAMAV_PROFILE, commands.SHARED_POSTGRES_PROFILE}

    missing = declared - run_scoped - set(commands.ALL_PROFILES)
    assert not missing, f"profiles a scenario can enable but teardown never removes: {sorted(missing)}"


def test_public_builtin_providers_are_off_at_boot_in_every_scenario() -> None:
    """The Gutenberg catalogue import starts with the host, so disabling the
    provider through the API after login is too late -- every scenario was
    downloading a large archive from gutenberg.org (and getting 504s)."""
    text = commands.COMPOSE_FILE.read_text(encoding="utf-8")

    assert "MetadataProviders__Gutenberg__Enabled=${FAMILY_LIBRARIAN_GUTENBERG_PROVIDER_ENABLED:-false}" in text
    assert "MetadataProviders__LibriVox__Enabled=${FAMILY_LIBRARIAN_LIBRIVOX_PROVIDER_ENABLED:-false}" in text



def _source_built_services_in_compose() -> set[str]:
    services: set[str] = set()
    current = None
    in_services = False
    for line in commands.COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(" "):
            in_services = line.startswith("services:")
            current = None
            continue
        if in_services and line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
        elif current and "FAMILY_LIBRARIAN_SOURCE_DIR" in line and not line.lstrip().startswith("#"):
            services.add(current)
    return services


def test_lab_run_rebuilds_every_image_built_from_the_product_source() -> None:
    """A source-built image missing from SOURCE_BUILT_SERVICES is never rebuilt:
    the sample-provider fixture kept a broken EPUB for a day after the fix."""
    import inspect

    declared = _source_built_services_in_compose()

    assert declared, "found no source-built services; the compose scan is broken"
    assert declared == set(commands.SOURCE_BUILT_SERVICES)
    assert "*SOURCE_BUILT_SERVICES" in inspect.getsource(commands.handle_run)


# ---- shared CWA ----------------------------------------------------------

_SHARED_CWA_ENV = {
    "FAMILY_LIBRARIAN_SCENARIO_CWA_PROFILE": "shared-cwa-elsewhere",
    "FAMILY_LIBRARIAN_SCENARIO_CWA_SFTP_KEY_PROFILE": "shared-cwa-elsewhere",
    "FAMILY_LIBRARIAN_SCENARIO_CWA_SFTP_PASSWORD_PROFILE": "shared-cwa-elsewhere",
    "FAMILY_LIBRARIAN_CWA_INGEST_SOURCE": "/lab/runtime/shared-cwa-ingest",
    "FAMILY_LIBRARIAN_SHARED_CWA_ACTIVE": "1",
}


def _cwa_scenario(profile: str = "cwa-local", env=_SHARED_CWA_ENV) -> commands._BaseScenario:
    return commands._BaseScenario({}, "CWA-L-01", keep=False, profiles=(profile,), shared_cwa_env=env)


@pytest.mark.parametrize("profile", ["cwa-local", "cwa-sftp-key", "cwa-sftp-password"])
def test_every_cwa_profile_uses_the_shared_cwa(profile: str) -> None:
    scenario = _cwa_scenario(profile)

    assert scenario._shared_cwa is True
    assert scenario._values["FAMILY_LIBRARIAN_SCENARIO_CWA_PROFILE"] == "shared-cwa-elsewhere"


def test_a_scenario_without_cwa_is_left_alone() -> None:
    scenario = _cwa_scenario("abs")

    assert scenario._shared_cwa is False
    assert "FAMILY_LIBRARIAN_CWA_INGEST_SOURCE" not in scenario._values


def test_cwa_operations_go_to_the_shared_project_and_others_stay_local() -> None:
    scenario = _cwa_scenario()

    for service in ("cwa", "cwa-mailpit"):
        values, project, profiles = scenario._target(service)
        assert project == commands.SHARED_CWA_PROJECT
        assert profiles == ("cwa-local",)
        # The shared project must see its CWA profiles enabled, never the
        # scenario overrides that disable them.
        assert "FAMILY_LIBRARIAN_SCENARIO_CWA_PROFILE" not in values
    _, project, _ = scenario._target("family-librarian")
    assert project == scenario.project_name


def test_exec_into_cwa_targets_the_shared_project() -> None:
    command, _ = _cwa_scenario()._cwa_exec("true")

    assert command[command.index("--project-name") + 1] == commands.SHARED_CWA_PROJECT
    assert command[-2:] == ["cwa", "true"]


def test_fl_reaches_the_shared_cwa_through_the_host_gateway() -> None:
    assert _cwa_scenario().cwa_internal_url == "http://lab-host-gateway:18083"
    assert _cwa_scenario(env=None).cwa_internal_url == "http://cwa:8083"


@pytest.mark.parametrize(("setting", "enabled"), [(None, True), ("true", True), ("false", False), ("0", False)])
def test_shared_cwa_can_be_switched_off_in_lab_env(setting, enabled, monkeypatch) -> None:
    values = {} if setting is None else {"FAMILY_LIBRARIAN_SHARED_CWA": setting}
    monkeypatch.setattr(commands, "_run_or_exit", lambda *a, **k: pytest.fail("must not start anything"))

    assert commands.shared_cwa_enabled(values) is enabled
    if not enabled:
        assert commands.ensure_shared_cwa(values) is None


# ---- replacing a shared CWA that stopped working -------------------------


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def cwa(monkeypatch, tmp_path):
    """A shared CWA whose faults are chosen per test; a replacement clears them unless told otherwise."""
    state = SimpleNamespace(
        unhealthy=False, silent=False, stuck_library=False, replacement_fixes=True, replacement_error=None,
        replaced=0, mail_cleared=0,
    )

    def compose(_values, _project, *arguments, **_kwargs):
        if arguments[0] == "up" and state.unhealthy:
            return _Done(1, stderr="container family-librarian-lab-shared-cwa-cwa-1 is unhealthy")
        if arguments[0] == "logs":
            return _Done(stdout="cwa-1 | relay password is hunter2-relay")
        return _Done()

    def replace_cwa(_values):
        state.replaced += 1
        if state.replacement_error:
            raise state.replacement_error
        if state.replacement_fixes:
            state.unhealthy = state.silent = state.stuck_library = False

    def empty(*_args):
        if state.stuck_library:
            raise AssertionError("Could not empty the shared CWA library (remaining='3'): database is locked")

    class Mailpit:
        def __init__(self, *_args) -> None:
            pass

        def clear(self) -> None:
            state.mail_cleared += 1

    monkeypatch.setattr(commands, "_compose", compose)
    monkeypatch.setattr(commands, "_start_shared_cwa", replace_cwa)
    monkeypatch.setattr(commands, "_shared_cwa_answers", lambda _: not state.silent)
    monkeypatch.setattr(commands, "_empty_cwa_library", empty)
    monkeypatch.setattr(commands.clients, "MailpitClient", Mailpit)
    monkeypatch.setattr(commands, "RESULTS_ROOT", tmp_path)
    monkeypatch.setattr(commands, "_SHARED_CWA_REPLACEMENTS", [])
    return state


def test_a_working_shared_cwa_is_not_replaced(cwa, tmp_path) -> None:
    commands.reset_shared_cwa({})

    assert (cwa.replaced, cwa.mail_cleared) == (0, 1)
    assert commands._SHARED_CWA_REPLACEMENTS == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fault, reason", [
    ("unhealthy", "is unhealthy"),
    ("silent", "did not answer a catalog search"),
    ("stuck_library", "database is locked"),
])
def test_a_stuck_shared_cwa_is_replaced_once_after_its_details_are_saved(cwa, fault, reason) -> None:
    setattr(cwa, fault, True)

    commands.reset_shared_cwa({"FAMILY_LIBRARIAN_CWA_SMTP_PASSWORD": "hunter2-relay"})

    assert (cwa.replaced, cwa.mail_cleared) == (1, 1)
    [details] = commands._SHARED_CWA_REPLACEMENTS
    assert reason in (details / "problem.txt").read_text()
    logs = (details / "compose-logs.txt").read_text()
    assert "hunter2-relay" not in logs and "[REDACTED]" in logs
    assert (details / "container-state.txt").is_file()
    # A diagnostics folder must not look like a run or a case folder to the code that searches for those.
    assert "-suites-" not in details.name and commands.LAB_PROJECT_PREFIX not in details.name


def test_a_cwa_that_is_still_stuck_after_replacement_fails_the_case(cwa) -> None:
    cwa.silent = True
    cwa.replacement_fixes = False

    with pytest.raises(AssertionError, match="still does not work after being replaced.*shared-cwa-diagnostics"):
        commands.reset_shared_cwa({})

    assert (cwa.replaced, cwa.mail_cleared) == (1, 0)


@pytest.mark.parametrize("error", [RuntimeError("compose up failed"), SystemExit("could not prepare ingest")])
def test_a_failed_replacement_fails_only_the_case(cwa, error) -> None:
    cwa.unhealthy = True
    cwa.replacement_error = error

    # SystemExit would end the whole run; a case failure must be an ordinary exception.
    with pytest.raises(AssertionError, match="could not be replaced"):
        commands.reset_shared_cwa({})


@pytest.mark.parametrize("replies, answers, requests", [
    ([True], True, 1),
    ([False, False, True], True, 3),
    ([False] * 10, False, 4),
])
def test_cwa_gets_a_few_chances_to_answer(monkeypatch, replies, answers, requests) -> None:
    asked: list[bool] = []

    def reply(_self, timeout: float = 15.0) -> bool:
        asked.append(True)
        return replies[len(asked) - 1]

    monkeypatch.setattr(commands.clients.CwaClient, "answers", reply)
    monkeypatch.setattr(commands.time, "sleep", lambda _: None)

    assert commands._shared_cwa_answers({}) is answers
    assert len(asked) == requests


@pytest.mark.parametrize(
    "status, answers", [(200, True), (401, True), (404, True), (500, False), (503, False), (0, False)])
def test_only_silence_or_a_server_error_means_cwa_is_not_answering(monkeypatch, status, answers) -> None:
    monkeypatch.setattr(commands.clients, "_http", lambda *_, **__: (status, b""))

    assert commands.clients.CwaClient(host_base_url="http://cwa").answers() is answers
