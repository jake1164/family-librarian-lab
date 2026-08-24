"""Base-profile lifecycle commands for the Family Librarian lab.

This module intentionally uses only se-lab's documented command-registration
extension point.  Compose topology, readiness semantics, and artifact content
are product-lab responsibilities.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from agent import common as lab_common, registry
from agent.suites import Suite, discover_suites, run_suites, suites_in_group

from family_librarian_lab import clients
from family_librarian_lab.api import FamilyLibrarianApi


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "compose.base.yaml"
LAB_ENV_FILE = REPO_ROOT / "lab.env"
RESULTS_ROOT = REPO_ROOT / "results"
TESTS_ROOT = REPO_ROOT / "tests"
PROFILE = "base"
ALL_PROFILES = (
    PROFILE,
    clients.CWA_PROFILE,
    clients.ABS_PROFILE,
    clients.CWA_SFTP_PROFILE_KEY,
    clients.CWA_SFTP_PROFILE_PASSWORD,
)
SFTP_KEY_DIR = REPO_ROOT / "runtime" / "sftp-test-key"
DEFAULT_HOST_PORT = 18080
DEFAULT_REPO_URL = "git@github.com:jake1164/family-librarian.git"
_SECRET_NAMES = (
    "FAMILY_LIBRARIAN_POSTGRES_PASSWORD",
    "FAMILY_LIBRARIAN_ADMIN_PASSWORD",
)


def _configure_checkout_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        help="Source branch or tag to check out (default: refresh the current checkout's branch)",
    )


def _configure_up(parser: argparse.ArgumentParser) -> None:
    _configure_checkout_target(parser)
    parser.add_argument(
        "--profile",
        nargs="+",
        metavar="CLIENT",
        default=[],
        choices=[clients.CWA_PROFILE, clients.ABS_PROFILE, clients.CWA_SFTP_PROFILE_KEY, clients.CWA_SFTP_PROFILE_PASSWORD],
        help="Extra destination(s) to bring up and wire alongside the base profile "
        "(cwa-local, abs, cwa-sftp-key, cwa-sftp-password)",
    )


def _configure_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-name", default=None, help="Compose project name (default: the lab's standard project name)")


def _configure_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--group", default="base", help="Suite group to run (default: base; use all for every suite)")
    parser.add_argument("--case", default=None, help="Run one registered case id (for example SEC-02)")
    parser.add_argument("--keep", action="store_true", help="Keep each failed/successful scenario project for investigation")
    parser.add_argument("--skip-build", action="store_true", help="Use the existing Family Librarian image without rebuilding it")


def _repo_url() -> str:
    """Plugin-level default: this plugin only ever tests Family Librarian, so it
    can just know that -- FAMILY_LIBRARIAN_REPO_URL in lab.env becomes an
    optional fork/mirror override rather than a required setting."""
    return lab_common.resolve_setting("FAMILY_LIBRARIAN_REPO_URL", default=DEFAULT_REPO_URL) or DEFAULT_REPO_URL


def _checkout_source(target: str | None) -> Path:
    """Land Family Librarian's source at repo_dir() -- a second, lab-managed
    clone separate from any manual dev checkout, same convention
    m3undle-lab-public already uses -- rather than requiring
    FAMILY_LIBRARIAN_SOURCE_DIR to point at an externally-managed one."""
    repo_url = _repo_url()
    lab_common.ensure_repo_checkout(repo_url)
    if target:
        kind, ref = lab_common.resolve_build_target(target, repo_url)
        if kind == "tag":
            lab_common.git_prepare_tag(ref)
        else:
            lab_common.git_prepare_branch(ref)
    else:
        lab_common.git_refresh_current_branch()
    return lab_common.repo_dir()


def _load_lab_env() -> dict[str, str]:
    if not LAB_ENV_FILE.is_file():
        raise SystemExit(f"Missing {LAB_ENV_FILE}. Copy lab.env.example to lab.env and set its required values.")

    values: dict[str, str] = {}
    for line in LAB_ENV_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        key, separator, value = text.partition("=")
        if not separator or not key:
            raise SystemExit(f"Invalid lab.env line: {line!r}")
        values[key] = value
    # Always the lab-managed checkout, not a manually-set external path -- see
    # _checkout_source(). Overrides any stale FAMILY_LIBRARIAN_SOURCE_DIR line
    # left in lab.env.
    values["FAMILY_LIBRARIAN_SOURCE_DIR"] = str(lab_common.repo_dir())
    # Idempotent (skips regeneration if already present) and cheap enough to run
    # on every command, the same way as the checkout refresh above -- keeps the
    # cwa-sftp-key bind-mount path always valid regardless of which profile a
    # given command actually ends up using, matching the "harmless when unused"
    # pattern already established for the always-mounted cwa-ingest volume.
    clients.ensure_sftp_test_keypair(SFTP_KEY_DIR)
    values["FAMILY_LIBRARIAN_SFTP_KEY_DIR"] = str(SFTP_KEY_DIR)
    values.setdefault("FAMILY_LIBRARIAN_SFTP_PASSWORD", clients.CWA_SFTP_DEFAULT_PASSWORD)
    return values


def _project_name(values: dict[str, str], requested: str | None, *, unique: bool) -> str:
    candidate = requested or values.get("FAMILY_LIBRARIAN_PROJECT_NAME")
    if not candidate and unique:
        candidate = "family-librarian-lab-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    if not candidate:
        candidate = lab_common.project_name()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", candidate):
        raise SystemExit("Compose project names may contain lowercase letters, digits, hyphens, and underscores only.")
    return candidate


def _compose(
    values: dict[str, str],
    project_name: str,
    *arguments: str,
    profiles: Sequence[str] = (PROFILE,),
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(values)
    command = [
        "docker",
        "compose",
        "--env-file",
        str(LAB_ENV_FILE),
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE_FILE),
    ]
    for profile in profiles:
        command += ["--profile", profile]
    command += list(arguments)
    return subprocess.run(command, env=environment, text=True, capture_output=capture, check=False)


def _run_or_exit(
    values: dict[str, str], project_name: str, *arguments: str, profiles: Sequence[str] = (PROFILE,)
) -> None:
    result = _compose(values, project_name, *arguments, profiles=profiles)
    if result.returncode:
        raise SystemExit(result.returncode)


def _host_base(values: dict[str, str]) -> str:
    return _client_host_base(values, DEFAULT_HOST_PORT, "FAMILY_LIBRARIAN_HOST_PORT")


def _client_host_base(values: dict[str, str], default_port: int, env_key: str) -> str:
    port = values.get(env_key, str(default_port))
    try:
        numeric_port = int(port)
    except ValueError as error:
        raise SystemExit(f"{env_key} must be a valid TCP port.") from error
    if not 1 <= numeric_port <= 65535:
        raise SystemExit(f"{env_key} must be between 1 and 65535.")
    return f"http://127.0.0.1:{numeric_port}"


def _wire_destinations(
    values: dict[str, str], profiles: Sequence[str], api: FamilyLibrarianApi
) -> tuple["clients.CwaClient | None", "clients.AbsClient | None", "dict[str, object] | None"]:
    """Configure Family Librarian to point at whichever extra destinations this
    scenario/deployment brought up -- the same call, used by both `up` (manual
    testing) and the cwa-local/abs/cwa-sftp-* suites' scenario setup (automated
    testing), so both paths exercise the identical wiring rather than two
    hand-maintained copies of it."""
    cwa_client: clients.CwaClient | None = None
    abs_client: clients.AbsClient | None = None
    sftp_wiring: dict[str, object] | None = None

    if clients.CWA_PROFILE in profiles:
        cwa_client = clients.CwaClient(
            host_base_url=_client_host_base(values, clients.CWA_DEFAULT_HOST_PORT, "FAMILY_LIBRARIAN_CWA_HOST_PORT")
        )
        api.configure_cwa_local(
            local_ingest_path=clients.CWA_INGEST_CONTAINER_PATH,
            opds_base_url=clients.CWA_INTERNAL_URL,
            opds_username=clients.CWA_DEFAULT_USERNAME,
            opds_password=clients.CWA_DEFAULT_PASSWORD,
        )
    elif clients.CWA_SFTP_PROFILE_KEY in profiles or clients.CWA_SFTP_PROFILE_PASSWORD in profiles:
        is_key_mode = clients.CWA_SFTP_PROFILE_KEY in profiles
        service = clients.CWA_SFTP_SERVICE_KEY if is_key_mode else clients.CWA_SFTP_SERVICE_PASSWORD
        if is_key_mode:
            credential, _ = clients.ensure_sftp_test_keypair(SFTP_KEY_DIR)
        else:
            credential = values["FAMILY_LIBRARIAN_SFTP_PASSWORD"]
        cwa_client = clients.CwaClient(
            host_base_url=_client_host_base(values, clients.CWA_DEFAULT_HOST_PORT, "FAMILY_LIBRARIAN_CWA_HOST_PORT")
        )
        sftp_wiring = api.configure_cwa_sftp(
            sftp_host=service,
            sftp_port=clients.CWA_SFTP_PORT,
            sftp_username=clients.CWA_SFTP_USERNAME,
            sftp_ingest_path=clients.CWA_SFTP_INGEST_PATH,
            auth_mode="PrivateKey" if is_key_mode else "Password",
            credential=credential,
            opds_base_url=clients.CWA_INTERNAL_URL,
            opds_username=clients.CWA_DEFAULT_USERNAME,
            opds_password=clients.CWA_DEFAULT_PASSWORD,
        )

    if clients.ABS_PROFILE in profiles:
        abs_client = clients.AbsClient(
            host_base_url=_client_host_base(values, clients.ABS_DEFAULT_HOST_PORT, "FAMILY_LIBRARIAN_ABS_HOST_PORT")
        )
        token, library_id, folder_id = abs_client.ensure_bootstrapped()
        api.configure_audiobookshelf(
            base_url=clients.ABS_INTERNAL_URL, library_id=library_id, folder_id=folder_id, api_token=token
        )

    return cwa_client, abs_client, sftp_wiring


def _probe(url: str) -> dict[str, object]:
    try:
        with urlopen(url, timeout=10) as response:  # noqa: S310 - the local URL is constructed above
            return {"url": url, "status": response.status, "ok": response.status == 200}
    except HTTPError as error:
        return {"url": url, "status": error.code, "ok": False, "error": str(error)}
    except URLError as error:
        return {"url": url, "status": None, "ok": False, "error": str(error.reason)}


def _redact(text: str, values: dict[str, str]) -> str:
    for name in _SECRET_NAMES:
        secret = values.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _capture_result(values: dict[str, str], project_name: str, checks: dict[str, object], outcome: str) -> Path:
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{project_name}"
    run_directory = RESULTS_ROOT / run_id
    run_directory.mkdir(parents=True, exist_ok=False)

    ps = _compose(values, project_name, "ps", "--all", "--format", "json", profiles=ALL_PROFILES, capture=True)
    config = _compose(values, project_name, "config", "--no-interpolate", profiles=ALL_PROFILES, capture=True)
    logs = _compose(values, project_name, "logs", "--no-color", profiles=ALL_PROFILES, capture=True)

    (run_directory / "compose-ps.json").write_text(_redact(ps.stdout, values), encoding="utf-8")
    (run_directory / "compose-config.yaml").write_text(_redact(config.stdout, values), encoding="utf-8")
    (run_directory / "compose-logs.txt").write_text(_redact(logs.stdout + logs.stderr, values), encoding="utf-8")

    image = values.get("FAMILY_LIBRARIAN_IMAGE", "family-librarian-lab:dev")
    image_inspect = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    record = {
        "run_id": run_id,
        "profile": PROFILE,
        "compose_project": project_name,
        "recorded_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": outcome,
        "health_checks": checks,
        "images": {"family_librarian": image, "repo_digests": image_inspect.stdout.strip() or None},
        "artifacts": {
            "compose_ps": "compose-ps.json",
            "compose_config": "compose-config.yaml",
            "compose_logs": "compose-logs.txt",
        },
    }
    result_path = run_directory / "results-base-readiness.json"
    result_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result_path


def _compose_service_health(values: dict[str, str], project_name: str) -> tuple[dict[str, object], bool]:
    result = _compose(values, project_name, "ps", "--all", "--format", "json", profiles=ALL_PROFILES, capture=True)
    services: dict[str, object] = {}
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        service = row.get("Service")
        if isinstance(service, str):
            services[service] = {
                "state": row.get("State"),
                "health": row.get("Health"),
                "exit_code": row.get("ExitCode"),
            }

    expected = {
        "postgres": ("running", "healthy"),
        "clamav": ("running", "healthy"),
        "family-librarian": ("running", "healthy"),
        "migrate": ("exited", None),
    }
    healthy = result.returncode == 0 and all(
        isinstance(services.get(name), dict)
        and services[name].get("state") == state
        and (health is None or services[name].get("health") == health)
        and (name != "migrate" or services[name].get("exit_code") == 0)
        for name, (state, health) in expected.items()
    )
    return services, healthy


def _readiness(values: dict[str, str], project_name: str) -> tuple[dict[str, object], bool]:
    """Destination checks (cwa/abs) are auto-detected from which containers
    this project actually has -- not from a caller-supplied profile list -- so
    `status`, which has no other record of what a given project brought up,
    reports correctly without special-casing."""
    base = _host_base(values)
    checks: dict[str, object] = {
        "live": _probe(f"{base}/health/live"),
        "ready": _probe(f"{base}/health/ready"),
    }
    services, services_ready = _compose_service_health(values, project_name)
    if clients.CWA_SERVICE in services:
        cwa_url = _client_host_base(values, clients.CWA_DEFAULT_HOST_PORT, "FAMILY_LIBRARIAN_CWA_HOST_PORT")
        checks["cwa"] = {"url": cwa_url, "ok": clients.CwaClient(host_base_url=cwa_url).ready()}
    if clients.ABS_SERVICE in services:
        abs_url = _client_host_base(values, clients.ABS_DEFAULT_HOST_PORT, "FAMILY_LIBRARIAN_ABS_HOST_PORT")
        checks["abs"] = {"url": abs_url, "ok": clients.AbsClient(host_base_url=abs_url).ready()}
    checks["compose_services"] = services
    passed = (
        all(bool(item.get("ok")) for name, item in checks.items() if name != "compose_services" and isinstance(item, dict))
        and services_ready
    )
    return checks, passed


def _wait_for_service(
    values: dict[str, str], project_name: str, service_name: str, *, timeout_seconds: int = 360
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        services, _ = _compose_service_health(values, project_name)
        service = services.get(service_name)
        if isinstance(service, dict) and service.get("state") == "running" and service.get("health") in ("healthy", ""):
            return
        time.sleep(2)
    raise AssertionError(f"{service_name} did not become ready within six minutes after restart.")


class _BaseScenario:
    """One fresh, disposable Compose project backing one suite case.

    `profiles` are the extra destination profiles (cwa-local, abs) this
    scenario needs beyond `base` -- teardown always requests ALL_PROFILES
    regardless, so a scenario that did bring up an extra destination never
    leaves it as an undiscovered, profile-filtered-out orphan (the same class
    of bug already found and fixed once this session in `clients down`).
    """

    def __init__(self, values: dict[str, str], test_id: str, *, keep: bool, profiles: Sequence[str] = ()) -> None:
        self._values = values
        self._test_id = test_id
        self._keep = keep
        self._profiles = (PROFILE, *profiles)
        suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        self.project_name = f"family-librarian-lab-{test_id.lower()}-{suffix}"
        self.api: FamilyLibrarianApi | None = None
        self.readiness_passed = False
        self.cwa_client: clients.CwaClient | None = None
        self.abs_client: clients.AbsClient | None = None
        self.cwa_sftp_wiring: dict[str, object] | None = None
        self._result_directory: Path | None = None

    def __enter__(self) -> "_BaseScenario":
        _run_or_exit(self._values, self.project_name, "down", "--volumes", "--remove-orphans", profiles=ALL_PROFILES)
        _run_or_exit(self._values, self.project_name, "up", "--wait", "--remove-orphans", profiles=self._profiles)
        checks, self.readiness_passed = _readiness(self._values, self.project_name)
        outcome = "pass" if self.readiness_passed else "fail"
        self._result_directory = _capture_result(self._values, self.project_name, checks, outcome).parent
        if not self.readiness_passed:
            raise AssertionError("Base profile failed readiness checks.")
        api = FamilyLibrarianApi(_host_base(self._values))
        api.authenticate(
            self._values["FAMILY_LIBRARIAN_ADMIN_EMAIL"],
            self._values["FAMILY_LIBRARIAN_ADMIN_PASSWORD"],
        )
        self.api = api
        self.cwa_client, self.abs_client, self.cwa_sftp_wiring = _wire_destinations(
            self._values, self._profiles, api
        )
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        if self._result_directory is not None and self.api is not None:
            trace_path = self._result_directory / "api-trace.json"
            trace_path.write_text(json.dumps(self.api.trace, indent=2) + "\n", encoding="utf-8")
        if not self._keep:
            result = _compose(
                self._values, self.project_name, "down", "--volumes", "--remove-orphans",
                profiles=ALL_PROFILES, capture=True,
            )
            if result.returncode:
                print(_redact(result.stderr, self._values), file=sys.stderr, end="")
        return False

    def stop_service(self, service_name: str) -> None:
        """Stop one service in this scenario's isolated Compose project.

        Fault scenarios deliberately use Compose lifecycle operations rather
        than mocks, so the same helper works for ClamAV, Family Librarian,
        CWA, Audiobookshelf, and either SFTP sidecar.
        """
        _run_or_exit(self._values, self.project_name, "stop", service_name, profiles=self._profiles)

    def start_service(self, service_name: str) -> None:
        _run_or_exit(self._values, self.project_name, "up", "--wait", service_name, profiles=self._profiles)
        _wait_for_service(self._values, self.project_name, service_name)

    def restart_service(self, service_name: str) -> None:
        self.stop_service(service_name)
        self.start_service(service_name)

    def stop_clamav(self) -> None:
        self.stop_service("clamav")

    def start_clamav(self) -> None:
        self.start_service("clamav")


class _BaseScenarioFactory:
    def __init__(self, values: dict[str, str], *, keep: bool, profiles: Sequence[str] = ()) -> None:
        self._values = values
        self._keep = keep
        self._profiles = profiles

    def __call__(self, test_id: str) -> _BaseScenario:
        return _BaseScenario(self._values, test_id, keep=self._keep, profiles=self._profiles)


@registry.command("build", help="Check out and build Family Librarian's base-profile image", configure=_configure_checkout_target)
def handle_build(args: argparse.Namespace, config: object) -> int:
    _checkout_source(args.target)
    values = _load_lab_env()
    project_name = lab_common.project_name()
    _run_or_exit(values, project_name, "build", "family-librarian", "migrate")
    return 0


@registry.command(
    "up",
    help="Check out, build, and deploy Family Librarian's base profile, and leave it running for manual testing",
    configure=_configure_up,
)
def handle_up(args: argparse.Namespace, config: object) -> int:
    _checkout_source(args.target)
    values = _load_lab_env()
    project_name = lab_common.project_name()
    profiles = (PROFILE, *args.profile)
    _run_or_exit(values, project_name, "up", "--build", "--wait", "--remove-orphans", profiles=profiles)
    checks, passed = _readiness(values, project_name)
    if not passed:
        print(json.dumps(checks, indent=2), file=sys.stderr)
        raise SystemExit("Base profile failed readiness checks after 'up'.")

    api = FamilyLibrarianApi(_host_base(values))
    api.authenticate(values["FAMILY_LIBRARIAN_ADMIN_EMAIL"], values["FAMILY_LIBRARIAN_ADMIN_PASSWORD"])
    cwa_client, abs_client, sftp_wiring = _wire_destinations(values, profiles, api)

    print(f"Family Librarian is up and healthy: {project_name}. Use './lab base down' when finished.", flush=True)
    if cwa_client is not None:
        print(
            f"  CWA:  {cwa_client.host_base_url}  (OPDS user: {clients.CWA_DEFAULT_USERNAME} / "
            f"password: {clients.CWA_DEFAULT_PASSWORD}) -- already wired into Family Librarian",
            flush=True,
        )
    if sftp_wiring is not None:
        print(
            "  CWA ingest transport: SFTP, trusted and enabled during this 'up' "
            "(see the trust probe in the run's own output above for detail).",
            flush=True,
        )
    if abs_client is not None:
        print(
            f"  Audiobookshelf: {abs_client.host_base_url}  (user: {clients.ABS_DEFAULT_USERNAME} / "
            f"password: {clients.ABS_DEFAULT_PASSWORD}) -- already wired into Family Librarian",
            flush=True,
        )
    return 0


@registry.command("status", help="Report base-profile Compose and HTTP health", configure=_configure_project)
def handle_status(args: argparse.Namespace, config: object) -> int:
    values = _load_lab_env()
    project_name = _project_name(values, args.project_name, unique=False)
    ps = _compose(values, project_name, "ps", "--all", "--format", "json", profiles=ALL_PROFILES, capture=True)
    if ps.stdout:
        print(ps.stdout, end="" if ps.stdout.endswith("\n") else "\n")
    if ps.returncode:
        print(ps.stderr, file=sys.stderr, end="" if ps.stderr.endswith("\n") else "\n")
        return ps.returncode
    checks, passed = _readiness(values, project_name)
    print(json.dumps({"health": checks}, indent=2), flush=True)
    return 0 if passed else 1


@registry.command("base down", help="Stop a base-profile project and remove its containers", configure=_configure_project)
def handle_down(args: argparse.Namespace, config: object) -> int:
    values = _load_lab_env()
    project_name = _project_name(values, args.project_name, unique=False)
    _run_or_exit(values, project_name, "down", "--remove-orphans", profiles=ALL_PROFILES)
    print(f"Base profile stopped: {project_name}", flush=True)
    return 0


def _select_suites(suites: list[Suite], *, case: str | None) -> list[Suite]:
    if not case:
        return suites
    selected: list[Suite] = []
    for candidate in suites:
        matching = [c for c in candidate.cases if c.test_id == case]
        if matching:
            selected.append(
                Suite(
                    name=candidate.name,
                    group=candidate.group,
                    order=candidate.order,
                    cases=matching,
                    setup_fn=candidate.setup_fn,
                    teardown_fn=candidate.teardown_fn,
                )
            )
    return selected


def _profiles_for(suite: Suite) -> tuple[str, ...]:
    """Derive the extra destination profile a suite's own declared group
    needs, so `--group cwa-local`/`--group abs`/`--group cwa-sftp-key`/
    `--group cwa-sftp-password` each bring up exactly the destination they
    test."""
    if suite.group in (
        clients.CWA_PROFILE,
        clients.ABS_PROFILE,
        clients.CWA_SFTP_PROFILE_KEY,
        clients.CWA_SFTP_PROFILE_PASSWORD,
    ):
        return (suite.group,)
    return ()


def _group_suites_by_profile(suites: list[Suite]) -> list[tuple[tuple[str, ...], list[Suite]]]:
    """Buckets a mixed selection (e.g. `--group all`) by which extra profile
    each suite's own group needs, preserving selection order within each
    bucket. Each bucket gets its own scenario factory -- sharing one factory
    (and its bundled destination wiring) across every suite in a mixed run
    would enable CWA/ABS for suites that specifically assert no destination
    is configured (confirmed for real: base-security's SEC-01/SEC-02 failed
    exactly this way under a single blanket-profile factory)."""
    buckets: dict[tuple[str, ...], list[Suite]] = {}
    order: list[tuple[str, ...]] = []
    for target in suites:
        key = _profiles_for(target)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(target)
    return [(key, buckets[key]) for key in order]


@registry.command(
    "run",
    help="Run black-box integration suites against fresh, isolated base-profile projects",
    configure=_configure_run,
)
def handle_run(args: argparse.Namespace, config: object) -> int:
    suites = _select_suites(suites_in_group(discover_suites(TESTS_ROOT), args.group), case=args.case)
    if not suites:
        selector = f" and case {args.case!r}" if args.case else ""
        raise SystemExit(f"No suites found for group {args.group!r}{selector}.")

    if not args.skip_build:
        _checkout_source(None)
    values = _load_lab_env()
    if not args.skip_build:
        build_project = lab_common.project_name()
        _run_or_exit(values, build_project, "build", "family-librarian", "migrate")

    selector = args.case.lower() if args.case else args.group
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-suites-{selector}"
    run_directory = RESULTS_ROOT / run_id
    run_directory.mkdir(parents=True, exist_ok=False)

    all_results = []
    failed = False
    for profiles, group_suites in _group_suites_by_profile(suites):
        factory = _BaseScenarioFactory(values, keep=args.keep, profiles=profiles)
        summary = run_suites(group_suites, results_dir=run_directory, scenario_factory=factory)
        all_results.extend(summary.results)
        failed = failed or summary.failed

    report = {
        "run_id": run_id,
        "profile": PROFILE,
        "group": args.group,
        "case": args.case,
        "outcome": "fail" if failed else "pass",
        "suites": [
            {
                "suite": result.suite_name,
                "expected": result.expected,
                "actual": result.actual,
                "setup_ok": result.setup_ok,
                "setup_error": result.setup_error,
                "drifted": result.drifted,
                "result": f"results-{result.suite_name}.json",
            }
            for result in all_results
        ],
    }
    summary_path = run_directory / "results-suite-run.json"
    summary_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Suite result captured: {summary_path}", flush=True)
    return 1 if failed else 0
