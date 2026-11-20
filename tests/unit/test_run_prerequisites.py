"""Run setup must be deterministic and reject broken prerequisites before cases run."""

from contextlib import nullcontext
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from agent.suites import Suite, run_suites
from family_librarian_lab import clients, commands


def test_run_ignores_manual_live_services_and_preserves_suite_fixtures():
    manual = {
        "FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED": "false",
        "FAMILY_LIBRARIAN_LOCAL_AUTH_ENABLED": "false",
        "FAMILY_LIBRARIAN_OPENLIBRARY_ENABLED": "true",
        "FAMILY_LIBRARIAN_GOOGLE_BOOKS_ENABLED": "true",
        "FAMILY_LIBRARIAN_GOOGLE_BOOKS_API_KEY": "personal-key",
        "FAMILY_LIBRARIAN_HARDCOVER_ENABLED": "true",
        "FAMILY_LIBRARIAN_GUTENBERG_PROVIDER_ENABLED": "true",
        "FAMILY_LIBRARIAN_LIBRIVOX_PROVIDER_ENABLED": "true",
        "FAMILY_LIBRARIAN_CONVERSATION_ENABLED": "true",
        "FAMILY_LIBRARIAN_CONVERSATION_WEB_ENABLED": "true",
        "FAMILY_LIBRARIAN_CONVERSATION_MATRIX_ENABLED": "true",
        "FAMILY_LIBRARIAN_MODEL_CAPTURE_REJECTED_REPLIES": "true",
        "FAMILY_LIBRARIAN_PUBLIC_ORIGIN": "https://manual.example.test",
        "FAMILY_LIBRARIAN_CWA_SMTP_HOST": "smtp.example.test",
        "FAMILY_LIBRARIAN_READER1_KINDLE_EMAIL": "personal@example.test",
        "FAMILY_LIBRARIAN_READER2_KINDLE_EMAIL": "personal2@example.test",
        clients.EXTERNAL_PROVIDER_QUESTION_FIXTURES_ENV: "1",
    }
    original = manual.copy()
    factory = commands._BaseScenarioFactory(manual, keep=False)
    factory.extra_env = {clients.EXTERNAL_PROVIDER_QUESTION_FIXTURES_ENV: "1", "SSL_CERT_FILE": "fixture-ca.pem"}
    scenario = factory("Q-01")
    values = scenario._values

    assert values["FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED"] == "true"
    assert values["FAMILY_LIBRARIAN_LOCAL_AUTH_ENABLED"] == "true"
    for key in ("FAMILY_LIBRARIAN_OPENLIBRARY_ENABLED", "FAMILY_LIBRARIAN_GOOGLE_BOOKS_ENABLED",
                "FAMILY_LIBRARIAN_HARDCOVER_ENABLED", "FAMILY_LIBRARIAN_GUTENBERG_PROVIDER_ENABLED",
                "FAMILY_LIBRARIAN_LIBRIVOX_PROVIDER_ENABLED", "FAMILY_LIBRARIAN_CONVERSATION_ENABLED",
                "FAMILY_LIBRARIAN_CONVERSATION_WEB_ENABLED", "FAMILY_LIBRARIAN_CONVERSATION_MATRIX_ENABLED",
                "FAMILY_LIBRARIAN_MODEL_CAPTURE_REJECTED_REPLIES"):
        assert values[key] == "false", key
    assert values["FAMILY_LIBRARIAN_GOOGLE_BOOKS_API_KEY"] == ""
    assert values["FAMILY_LIBRARIAN_PUBLIC_ORIGIN"] == ""
    assert not commands._cwa_relay_settings(values).is_real
    assert not values["FAMILY_LIBRARIAN_READER1_KINDLE_EMAIL"]
    assert not values["FAMILY_LIBRARIAN_READER2_KINDLE_EMAIL"]
    assert values[clients.EXTERNAL_PROVIDER_QUESTION_FIXTURES_ENV] == "1"
    assert values["SSL_CERT_FILE"] == "fixture-ca.pem"
    assert factory._values[clients.EXTERNAL_PROVIDER_QUESTION_FIXTURES_ENV] == "0"
    assert manual == original


def test_suite_state_does_not_leak_but_run_scanner_settings_survive(tmp_path):
    factory = commands._BaseScenarioFactory({}, keep=False)
    factory.shared_scanner_env = {"FAMILY_LIBRARIAN_CLAMAV_HOST": "run-scanner", "FAMILY_LIBRARIAN_CLAMAV_PORT": "13310"}
    first = Suite("first", extra_profiles=(clients.CWA_PROFILE,))
    second = Suite("second")

    @first.setup
    def setup(scenario_factory):
        scenario_factory.extra_env = {"SSL_CERT_FILE": "first-suite.pem"}
        scenario_factory.seed_readers = True
        scenario_factory.local_database = True

    @first.case("FIRST")
    def first_case(ctx, scenario_factory):
        assert scenario_factory("FIRST")._values["SSL_CERT_FILE"] == "first-suite.pem"
        ctx.ok("FIRST", "configured")

    @second.case("SECOND")
    def second_case(ctx, scenario_factory):
        scenario = scenario_factory("SECOND")
        assert scenario_factory.seed_readers is False
        assert scenario_factory.local_database is False
        assert "SSL_CERT_FILE" not in scenario._values
        assert scenario._values["FAMILY_LIBRARIAN_CLAMAV_HOST"] == "run-scanner"
        assert scenario._profiles == (commands.PROFILE,)
        ctx.ok("SECOND", "isolated")

    result = run_suites(commands._scoped_for_run([first, second], factory), results_dir=tmp_path,
                        live_progress=False, scenario_factory=factory)
    assert not result.failed
    assert sum(s.passed for s in result.results) == 2


class _Api:
    def __init__(self, providers=None, fail_resolve=False):
        self.providers = providers if providers is not None else [
            {"providerId": p, "isEnabled": p == "demo", "isMisconfigured": False}
            for p in ("demo", "openlibrary", "googlebooks", "hardcover", "gutendex", "librivox")]
        self.resolved = []
        self.fail_resolve = fail_resolve

    def metadata_providers(self):
        return self.providers

    def request_formats(self):
        return {"ebook": {"isEnabled": False, "isReady": False},
                "audiobook": {"isEnabled": False, "isReady": False}}

    def resolve_demo_work(self, slug):
        self.resolved.append(slug)
        if self.fail_resolve:
            raise AssertionError("demo resolution returned HTTP 409")
        return "fixture-work-id"


def test_prerequisites_resolve_every_registered_demo_fixture():
    api = _Api()
    evidence = commands._verify_catalog_prerequisites(api)
    assert set(api.resolved) == set(commands._REQUIRED_DEMO_SLUGS)
    assert "the-hobbit" in api.resolved and "cinder-gate" in api.resolved
    assert evidence["demo_slugs"] == api.resolved


@pytest.mark.parametrize("providers, message", [
    ([], "missing"),
    ([{**p, "isEnabled": False} for p in _Api().providers], "disabled"),
    (_Api().providers + [{"providerId": "new-live-provider", "isEnabled": True}], "new-live-provider"),
])
def test_missing_or_live_providers_stop_before_resolving(providers, message):
    api = _Api(providers)
    with pytest.raises(AssertionError, match=message):
        commands._verify_catalog_prerequisites(api)
    assert api.resolved == []


def test_missing_request_format_contract_stops_before_resolving():
    api = _Api()
    api.request_formats = lambda: {"ebook": {"isEnabled": False}, "audiobook": {"isEnabled": False}}
    with pytest.raises(AssertionError, match="ebook.*missing or invalid"):
        commands._verify_catalog_prerequisites(api)
    assert not api.resolved


def test_run_preflight_failure_is_saved_and_disposable_even_with_keep(monkeypatch, tmp_path):
    entered = []

    class Scenario:
        def __init__(self, values, test_id, **kwargs):
            assert kwargs["keep"] is False
            self.api = _Api(fail_resolve=True)
            self._result_directory = tmp_path / "artifacts"
            self.project_name = "preflight-project"

        def __enter__(self):
            entered.append(True)
            return self

        def __exit__(self, *_args):
            entered.append(False)

    monkeypatch.setattr(commands, "_BaseScenario", Scenario)
    factory = commands._BaseScenarioFactory({}, keep=True)
    assert commands._run_prerequisites(factory, tmp_path) is False
    evidence = json.loads((tmp_path / "results-run-prerequisites.json").read_text())
    assert evidence["outcome"] == "fail"
    assert "HTTP 409" in evidence["error"]
    assert entered == [True, False]


def test_run_stops_before_main_suites_and_cleans_shared_services_when_preflight_fails(monkeypatch, tmp_path):
    stopped = []
    monkeypatch.setattr(commands, "_describe_run_plan", lambda _: SimpleNamespace(confirm=lambda **_: True))
    monkeypatch.setattr(commands, "discover_suites", lambda _: [Suite("selected")])
    monkeypatch.setattr(commands, "select_suites", lambda suites, **_: suites)
    monkeypatch.setattr(commands.lab_common, "run_lock", lambda **_: nullcontext())
    monkeypatch.setattr(commands, "_load_lab_env", lambda: {})
    monkeypatch.setattr(commands, "_check_no_conflicting_containers", lambda _: None)
    monkeypatch.setattr(commands, "RESULTS_ROOT", tmp_path)
    monkeypatch.setattr(commands, "ensure_shared_postgres", lambda _: None)
    monkeypatch.setattr(commands, "ensure_shared_cwa", lambda _: None)
    monkeypatch.setattr(commands, "ensure_shared_clamav", lambda: {})
    monkeypatch.setattr(commands, "_run_prerequisites", lambda *_: False)
    monkeypatch.setattr(commands, "run_suites", lambda *_, **__: pytest.fail("main cases must not run"))
    monkeypatch.setattr(commands, "_stop_shared_cwa", lambda _: stopped.append("cwa"))
    monkeypatch.setattr(commands, "_stop_shared_postgres", lambda _: stopped.append("postgres"))
    monkeypatch.setattr(commands, "_stop_shared_clamav", lambda: stopped.append("clamav"))
    reports = []
    monkeypatch.setattr(commands, "_write_se_lab_report_record", lambda **kwargs: reports.append(kwargs))
    monkeypatch.setattr(commands.lab_common, "is_git_checkout", lambda _: False)
    monkeypatch.setattr(commands.lab_common, "repo_dir", lambda: tmp_path)
    args = SimpleNamespace(target=None, skip_build=True, yes=True, test_group="all", case=None, keep=False)
    assert commands.handle_run(args, None) == 1
    assert stopped == ["cwa", "postgres", "clamav"]
    assert commands._RUN_OWNS_SHARED_SERVICES is False
    summary = json.loads(next(tmp_path.glob("*/results-suite-run.json")).read_text())
    assert summary["outcome"] == "fail"
    assert summary["suites"][0]["suite"] == "run-prerequisites"
    assert reports[0]["failed"] is True


@pytest.mark.parametrize("ready", [True, False])
def test_destination_readiness_is_checked_before_case_mutations(ready):
    api = SimpleNamespace(request_formats=lambda: {
        "ebook": {"isReady": ready, "reason": "Connection test required"},
        "audiobook": {"isReady": True},
    })
    if ready:
        commands._verify_destination_readiness(api, (clients.CWA_PROFILE, clients.ABS_PROFILE))
    else:
        with pytest.raises(AssertionError, match="ebook.*Connection test required"):
            commands._verify_destination_readiness(api, (clients.CWA_PROFILE,))


def test_all_registered_suites_have_audited_profiles_and_fixture_setup(monkeypatch, tmp_path):
    expected_profiles = {
        "base-security": {"cwa-local"}, "base-restart": {"cwa-local"}, "backup-restore": {"cwa-local"},
        "cwa-local": {"cwa-local"}, "abs": {"abs"}, "abs-bundle": {"abs", "gutenberg"},
        "cwa-sftp-key": {"cwa-sftp-key"}, "cwa-sftp-password": {"cwa-sftp-password"},
        "smtp": {"smtp"}, "communications": {"smtp", "cwa-local"},
        "matrix": {"matrix", "cwa-local", "smtp"}, "gutenberg": {"gutenberg", "cwa-local"},
        "external-provider": {"external-provider", "cwa-local"},
        "acquisition-questions": {"external-provider", "cwa-local", "abs"},
        "acquisition-questions-matrix": {"matrix", "cwa-local", "external-provider"},
        "search-review": {"search-review", "cwa-local"},
        "candidate-triage": {"search-review", "cwa-local", "abs"},
        "artifact-decision": {"search-review", "cwa-local", "abs"},
        "artifact-release": {"search-review", "cwa-local", "abs"},
        "artifact-excerpt": {"search-review", "cwa-local", "abs"},
    }
    suites = commands.discover_suites(commands.TESTS_ROOT)
    assert {s.name for s in suites} == expected_profiles.keys(), "Audit prerequisites when registering a new suite"
    factory = commands._BaseScenarioFactory({}, keep=False)
    factory.shared_scanner_env = {"FAMILY_LIBRARIAN_CLAMAV_HOST": "shared-scanner"}
    checked = []

    def make_probe(target):
        def probe(ctx, scenario_factory):
            scenario = scenario_factory("SETUP-AUDIT")
            assert set(scenario._profiles) == {commands.PROFILE, *expected_profiles[target.name]}
            assert scenario._values["FAMILY_LIBRARIAN_DEMO_PROVIDER_ENABLED"] == "true"
            assert scenario._values["FAMILY_LIBRARIAN_CLAMAV_HOST"] == "shared-scanner"
            assert scenario_factory.local_database is (target.name == "backup-restore")
            assert scenario_factory.seed_readers is (target.name in {
                "cwa-local", "matrix", "acquisition-questions", "acquisition-questions-matrix"})
            questions = target.name in {"acquisition-questions", "acquisition-questions-matrix"}
            assert scenario._values[clients.EXTERNAL_PROVIDER_QUESTION_FIXTURES_ENV] == ("1" if questions else "0")
            needs_tls = bool(expected_profiles[target.name] & {"smtp", "gutenberg"})
            assert ("SSL_CERT_FILE" in scenario._values) is needs_tls
            checked.append(target.name)
            ctx.ok("SETUP-AUDIT", "suite setup verified")
        return probe

    audited = []
    for target in suites:
        globals_ = target.setup_fn.__globals__
        # Keep each suite's real setup/teardown; replace only Docker/TLS resource allocation.
        monkeypatch.setitem(globals_, "ensure_shared_clamav", lambda: factory.shared_scanner_env.copy())
        monkeypatch.setitem(globals_, "teardown_shared_clamav", lambda: None)
        monkeypatch.setitem(globals_, "ensure_smtp_fixture_tls", lambda: {"SSL_CERT_FILE": "smtp-fixture.pem"})
        monkeypatch.setitem(globals_, "ensure_gutenberg_fixture_tls", lambda: {"SSL_CERT_FILE": "gutenberg-fixture.pem"})
        audited.append(replace(target, cases=[replace(target.cases[0], func=make_probe(target))]))
    result = run_suites(commands._scoped_for_run(audited, factory), results_dir=tmp_path,
                        live_progress=False, scenario_factory=factory)
    assert not result.failed
    assert set(checked) == expected_profiles.keys()
