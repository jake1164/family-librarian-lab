"""Base-profile lifecycle commands for the Family Librarian lab.

This module intentionally uses only se-lab's documented command-registration
extension point.  Compose topology, readiness semantics, and artifact content
are product-lab responsibilities.
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import re
import select
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from agent import common as lab_common, registry
from agent.planning import RunPlan, RunReport
from agent.suites import CaseFunc, Suite, discover_suites, run_suites, select_suites

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
    # RawDescriptionHelpFormatter so the example block below keeps its own
    # line breaks/indentation instead of argparse re-wrapping it -- only
    # affects this subparser's description/epilog, not its per-argument help
    # text (still wrapped normally) or any other command's --help.
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = """\
Examples:
  ./lab up                                # base only, refresh the current branch
  ./lab up main                           # check out, build, and deploy `main`
  ./lab up main --profile cwa-local abs   # main, wired to both CWA and Audiobookshelf
  ./lab up --profile cwa-sftp-key         # current branch, CWA over SFTP (key auth)
  ./lab base down                         # tear down when done -- NOT `./lab down`

--profile takes multiple values in one flag (--profile cwa-local abs).
cwa-local and cwa-sftp-key/cwa-sftp-password are mutually exclusive -- they
wire the same underlying `cwa` service over a different ingest transport.

Connection info once up (defaults; override the *_HOST_PORT vars in lab.env):
  Family Librarian  http://127.0.0.1:18080  FAMILY_LIBRARIAN_ADMIN_EMAIL / _ADMIN_PASSWORD
  CWA               http://127.0.0.1:18083  admin / admin123
  Audiobookshelf    http://127.0.0.1:18378  bootstrapped and wired in automatically

`target` is lab-managed and fetched from GitHub -- push a local branch before
`./lab up <branch>` can see it. `./lab status` reports health at any time.
"""


def _configure_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-name", default=None, help="Compose project name (default: the lab's standard project name)")


def _configure_run(parser: argparse.ArgumentParser) -> None:
    _configure_checkout_target(parser)
    parser.add_argument("--test-group", default="base", help="Suite group to run (default: base; use all for every suite)")
    parser.add_argument("--case", default=None, help="Run one registered case id (for example SEC-02)")
    parser.add_argument("--keep", action="store_true", help="Keep each failed/successful scenario project for investigation")
    parser.add_argument("--skip-build", action="store_true", help="Use the existing Family Librarian image without rebuilding it")
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip the run-plan confirmation prompt (for CI/automation)"
    )


def _validate_run_options(args: argparse.Namespace) -> None:
    if args.target and args.skip_build:
        raise SystemExit("A source target and --skip-build are mutually exclusive.")


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
        # Shell environment wins over lab.env, matching agent.common.resolve_setting()'s
        # env > file > default precedence used everywhere else in se-lab -- a
        # CI/non-interactive host that sets FAMILY_LIBRARIAN_* only via the process
        # environment must not have that silently overridden by a stale lab.env line
        # once _compose() merges this dict on top of os.environ.
        values[key] = os.environ.get(key, value)
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
    values.setdefault(
        "FAMILY_LIBRARIAN_SFTP_PASSWORD",
        os.environ.get("FAMILY_LIBRARIAN_SFTP_PASSWORD", clients.CWA_SFTP_DEFAULT_PASSWORD),
    )
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
    if capture:
        return subprocess.run(command, env=environment, text=True, capture_output=True, check=False)

    # Not asked to capture -- normally this inherits the real terminal
    # directly (the right behavior for `up`/`build`/`status`, run
    # interactively with no dashboard around). But every scenario `run`
    # drives brings up a fresh Compose stack via this same function, and a
    # dashboard is active for the whole run then: docker compose's own
    # output can't be handed the real terminal directly while a live footer
    # owns the bottom of the screen. This call bypasses common.run() entirely
    # (its own project-name/profile plumbing), so it delegates to se-lab's
    # shared dashboard-aware streaming helper explicitly -- see
    # agent/common.py's stream_subprocess_to_dashboard(), which prints the
    # command's combined stdout/stderr live, through the dashboard's own
    # console, as it arrives.
    dashboard = lab_common.active_dashboard()
    if dashboard is not None:
        return lab_common.stream_subprocess_to_dashboard(dashboard, command, env=environment, check=False)
    return subprocess.run(command, env=environment, text=True, capture_output=False, check=False)


@dataclass(slots=True)
class _CwaIngestObserver:
    """A read-only polling observer for the CWA-facing side of the shared
    ingest volume.

    CWA-L-04 needs to observe the transport boundary while the host writes,
    without treating CWA's managed library or database as a test oracle.  The
    observer runs inside the real CWA container and only lists/stat's the
    shared ingest mount; it never writes to it.
    """

    process: subprocess.Popen[str]
    observations: list[tuple[str, int]] = field(default_factory=list)

    def wait_until_ready(self) -> None:
        if self.process.stdout is None:
            raise AssertionError("CWA ingest observer did not expose stdout.")
        ready, _, _ = select.select([self.process.stdout], [], [], 15)
        if not ready:
            self.stop()
            raise AssertionError("CWA ingest observer did not start within 15 seconds.")
        if self.process.stdout.readline().strip() != "__observer_ready__":
            self.stop()
            raise AssertionError("CWA ingest observer did not report its ready marker.")

    def stop(self) -> list[tuple[str, int]]:
        if self.process.poll() is None:
            if self.process.stdin is None:
                raise AssertionError("CWA ingest observer did not expose stdin.")
            self.process.stdin.write("stop\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)

        output = self.process.stdout.read() if self.process.stdout is not None else ""

        for line in output.splitlines():
            self._record(line)
        return self.observations

    def wait_for_uploading(self, timeout_seconds: float) -> bool:
        """Wait until the real SFTP transport has exposed its temporary
        remote filename, proving an interruption targets an active transfer
        rather than a before-connect or after-completion race."""
        if self.process.stdout is None:
            raise AssertionError("CWA ingest observer did not expose stdout.")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [], min(0.25, deadline - time.monotonic()))
            if not ready:
                continue
            line = self.process.stdout.readline()
            if not line:
                break
            observation = self._record(line)
            if observation is not None and observation[0].startswith(".") and observation[0].endswith(".uploading"):
                return True
        return False

    def _record(self, line: str) -> tuple[str, int] | None:
        filename, separator, size = line.strip().partition("\t")
        if not separator:
            return None
        try:
            observation = (filename, int(size))
        except ValueError:
            return None
        self.observations.append(observation)
        return observation


def _run_or_exit(
    values: dict[str, str], project_name: str, *arguments: str, profiles: Sequence[str] = (PROFILE,)
) -> None:
    """Run a compose command; raise with a clear message on failure.

    A plain Exception, not SystemExit -- SystemExit(result.returncode) used
    to be raised here, and two things about that were both wrong at once:
    an int SystemExit prints nothing at all (confirmed for real: a scenario's
    `up` failing this way looked like the whole run silently vanished, with
    the dashboard's own teardown the only visible trace), and SystemExit is a
    BaseException se-lab's run_suite() deliberately does not catch (so one
    case's bug can't crash the whole run) -- it killed the entire `run`
    instead of getting recorded as a failed test/setup like every other
    scenario failure, e.g. the AssertionError two lines below in
    _BaseScenario.__enter__. _compose() already prints the actual docker
    output above this by the time it's raised.
    """
    result = _compose(values, project_name, *arguments, profiles=profiles)
    if result.returncode:
        raise RuntimeError(
            f"`docker compose {' '.join(arguments)}` failed for project {project_name!r} "
            f"(exit {result.returncode}); see output above."
        )


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
    for row in lab_common.parse_compose_ps_json(result.stdout):
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

    def kill_service(self, service_name: str) -> None:
        """Abruptly terminate one disposable scenario service.

        Unlike ``stop_service``, Compose does not give the process a graceful
        shutdown window. Fault tests use this only when they need a genuine
        in-flight transport disconnect.
        """
        _run_or_exit(self._values, self.project_name, "kill", service_name, profiles=self._profiles)

    def start_service(self, service_name: str) -> None:
        _run_or_exit(self._values, self.project_name, "up", "--wait", service_name, profiles=self._profiles)
        _wait_for_service(self._values, self.project_name, service_name)

    def restart_service(self, service_name: str) -> None:
        self.stop_service(service_name)
        self.start_service(service_name)

    def observe_cwa_ingest(self) -> _CwaIngestObserver:
        """Poll CWA's read-only view of the shared ingest volume.

        This deliberately observes only filenames and byte lengths.  CWA's
        OPDS catalog remains the proof of a successful import.
        """
        command = [
            "docker",
            "compose",
            "--env-file",
            str(LAB_ENV_FILE),
            "--project-name",
            self.project_name,
            "--file",
            str(COMPOSE_FILE),
        ]
        for profile in self._profiles:
            command += ["--profile", profile]
        command += [
            "exec",
            "-T",
            clients.CWA_SERVICE,
            "sh",
            "-c",
            """
(read -r _; exit 0) &
stopper=$!
printf '%s\\n' '__observer_ready__'
while kill -0 "$stopper" 2>/dev/null; do
  for path in /cwa-book-ingest/.*.uploading /cwa-book-ingest/*.epub; do
    [ -f \"$path\" ] || continue
    printf '%s\\t%s\\n' \"${path##*/}\" \"$(wc -c < \"$path\")\"
  done
  sleep 0.01
done
""",
        ]
        environment = os.environ.copy()
        environment.update(self._values)
        observer = _CwaIngestObserver(
            subprocess.Popen(
                command,
                env=environment,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
        observer.wait_until_ready()
        return observer

    def seed_cwa_ingest(self, content: bytes, filename: str) -> None:
        """Place a fixture through CWA's own watched ingest path.

        This is deliberately an exec into CWA's ingest mount, never a write to
        its Calibre library or metadata database. CWA's watcher must still
        import it and expose it through OPDS before a scenario can pass.
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.epub", filename):
            raise ValueError("CWA seed filenames must be a simple .epub basename.")
        command = [
            "docker", "compose", "--env-file", str(LAB_ENV_FILE), "--project-name", self.project_name,
            "--file", str(COMPOSE_FILE),
        ]
        for profile in self._profiles:
            command += ["--profile", profile]
        command += [
            "exec", "-T", clients.CWA_SERVICE, "sh", "-c",
            f"base64 -d > /cwa-book-ingest/{filename}",
        ]
        environment = os.environ.copy()
        environment.update(self._values)
        result = subprocess.run(
            command,
            env=environment,
            input=base64.b64encode(content).decode("ascii"),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(f"CWA ingest seed failed: {_redact(result.stderr, self._values).strip()}")

    def cwa_ingest_filenames(self) -> list[str]:
        """Read only the CWA-facing shared ingest mount for fault diagnostics.
        Catalog visibility remains the success assertion for every CWA test."""
        command = [
            "docker", "compose", "--env-file", str(LAB_ENV_FILE), "--project-name", self.project_name,
            "--file", str(COMPOSE_FILE),
        ]
        for profile in self._profiles:
            command += ["--profile", profile]
        command += [
            "exec", "-T", clients.CWA_SERVICE, "sh", "-c",
            "for path in /cwa-book-ingest/.*.uploading /cwa-book-ingest/*.epub; do [ -f \"$path\" ] && basename \"$path\"; done; exit 0",
        ]
        environment = os.environ.copy()
        environment.update(self._values)
        result = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
        if result.returncode:
            raise AssertionError(f"Could not inspect CWA ingest files: {_redact(result.stderr, self._values).strip()}")
        return [line for line in result.stdout.splitlines() if line]

    def wait_for_cwa_ingest_uploading(self, timeout_seconds: float) -> bool:
        """Poll CWA's mount until the remote SFTP temporary file is visible.

        This is deliberately a sequence of short, independent Compose execs
        rather than a long-lived observer: Docker Desktop may detach stdin
        from a `compose exec -T` child, which makes an stdin-controlled watcher
        unsuitable for synchronizing a destructive transport fault.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if any(name.startswith(".") and name.endswith(".uploading") for name in self.cwa_ingest_filenames()):
                return True
            time.sleep(0.1)
        return False

    def regenerate_sftp_host_keys(self, service_name: str) -> None:
        """Rotate a disposable atmoz/sftp sidecar's server host keys.

        The service uses its real entrypoint to generate the replacement keys
        on restart; only this throwaway scenario container is altered.
        """
        _run_or_exit(
            self._values,
            self.project_name,
            "exec",
            "-T",
            service_name,
            "sh",
            "-c",
            # atmoz/sftp's entrypoint generates RSA and ed25519 keys, but
            # OpenSSH's default config may still list ECDSA as well. Recreate
            # the complete host-key set before restart so the deliberate
            # rotation does not turn into a sidecar bootstrap failure.
            "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A",
            profiles=self._profiles,
        )
        self.restart_service(service_name)

    def reauthenticate(self) -> None:
        """Refresh the browser-session client after a host restart."""
        if self.api is None:
            raise AssertionError("Scenario API client was not initialized.")
        self.api.authenticate(
            self._values["FAMILY_LIBRARIAN_ADMIN_EMAIL"],
            self._values["FAMILY_LIBRARIAN_ADMIN_PASSWORD"],
        )

    def stop_clamav(self) -> None:
        self.stop_service("clamav")

    def start_clamav(self) -> None:
        self.start_service("clamav")


class _BaseScenarioFactory:
    """Builds each case's isolated scenario using whichever compose
    profile(s) are currently active -- see `_scoped_for_run()`, which sets
    `active_profiles` to the right value for whatever suite is executing
    right now before each of its setup/case/teardown calls runs.

    This indirection (rather than keying a lookup table by test_id) is what
    lets `handle_run()` pass an entire mixed selection -- suites needing
    different real destinations -- to a single `run_suites()` call, so
    se-lab's dashboard/summary can track progress across the whole selection
    in one continuous run (see m3undle_lab's 22-suite run) instead of
    family-librarian-lab splitting it into several separate `run_suites()`
    calls (and several separate dashboards) just because some suites need
    `cwa-local`/`abs`/etc. and others don't. A plain test_id-keyed table
    can't do this safely: test_id isn't unique across suites (CWA-S-02
    exists in both the cwa-sftp-key and cwa-sftp-password suites, each
    needing a different profile), so two suites in the same selection can
    legitimately want the same test_id to mean different profiles.
    """

    def __init__(self, values: dict[str, str], *, keep: bool) -> None:
        self._values = values
        self._keep = keep
        self.active_profiles: tuple[str, ...] = ()

    def __call__(self, test_id: str) -> _BaseScenario:
        return _BaseScenario(self._values, test_id, keep=self._keep, profiles=self.active_profiles)


def _scoped_for_run(suites: Sequence[Suite], factory: _BaseScenarioFactory) -> list[Suite]:
    """Returns `suites` with every setup/case/teardown function wrapped so
    `factory.active_profiles` is set to that specific suite's own required
    profile(s) (`_profiles_for()`) for the duration of the call, then
    restored -- see `_BaseScenarioFactory` for why this, not a test_id-keyed
    table, is what safely lets suites needing different destinations share
    one `run_suites()` call. Cases run one at a time (`run_suite()` is a
    plain for-loop, no concurrency), so a single shared mutable attribute on
    the factory is safe."""

    def _bind(func: CaseFunc, profiles: tuple[str, ...]) -> CaseFunc:
        @functools.wraps(func)
        def wrapped(*args: object, **kwargs: object) -> object:
            previous = factory.active_profiles
            factory.active_profiles = profiles
            try:
                return func(*args, **kwargs)
            finally:
                factory.active_profiles = previous

        return wrapped

    scoped: list[Suite] = []
    for target in suites:
        profiles = _profiles_for(target)
        scoped.append(
            replace(
                target,
                cases=[replace(case, func=_bind(case.func, profiles)) for case in target.cases],
                setup_fn=_bind(target.setup_fn, profiles) if target.setup_fn is not None else None,
                teardown_fn=_bind(target.teardown_fn, profiles) if target.teardown_fn is not None else None,
            )
        )
    return scoped


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
    # Only guards this command's own setup against overlapping another
    # concurrent `up`/`run` on the same shared DEFAULT_HOST_PORT -- the lock
    # releases once setup finishes; the stack itself is deliberately left
    # running for manual testing (see './lab base down' below), independent
    # of this invocation's lifetime.
    with lab_common.run_lock(label="lab up"):
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


def _profiles_for(suite: Suite) -> tuple[str, ...]:
    """Derive the extra destination profile(s) a suite's scenario factory
    needs to bring up.

    A suite that declares its own `extra_profiles` (e.g. a "base"-group
    suite that still needs a real, then-stopped CWA destination) always wins
    -- that's an explicit, per-suite requirement independent of `group`.
    Otherwise fall back to the group-as-profile convention so
    `--test-group cwa-local`/`--test-group abs`/`--test-group cwa-sftp-key`/
    `--test-group cwa-sftp-password` each bring up exactly the destination
    they test."""
    if suite.extra_profiles:
        return suite.extra_profiles
    if suite.group in (
        clients.CWA_PROFILE,
        clients.ABS_PROFILE,
        clients.CWA_SFTP_PROFILE_KEY,
        clients.CWA_SFTP_PROFILE_PASSWORD,
    ):
        return (suite.group,)
    return ()


def _describe_run_plan(args: argparse.Namespace) -> RunPlan:
    plan = RunPlan(label="Family Librarian Lab", host=lab_common.current_hostname())
    if lab_common.is_git_checkout(lab_common.repo_dir()):
        branch = lab_common.repo_current_branch() or "detached"
        commit = lab_common.repo_head_commit(short=True) or "unknown"
        plan.add("Current checkout", f"branch={branch} commit={commit}")
    else:
        plan.add("Current checkout", "none yet")

    if args.skip_build:
        plan.add("Resolved source", "existing image (no checkout/build)")
        plan.add("Source action", "reuse currently built image")
    elif args.target:
        plan.add("Resolved source", f"branch or tag {args.target!r}")
        plan.add("Source action", "fetch origin, checkout/reset, build image")
    else:
        plan.add("Resolved source", "refresh current branch")
        plan.add("Source action", "fetch origin, fast-forward current branch, build image")

    selection = f"group {args.test_group}"
    if args.case:
        selection += f", case {args.case}"
    plan.add("Suites", selection)
    plan.add(
        "Scenario cleanup",
        "keep each scenario project (--keep)" if args.keep else "remove each scenario project after its case",
    )
    return plan


@registry.command(
    "run",
    help="Run black-box integration suites against fresh, isolated base-profile projects",
    configure=_configure_run,
)
def handle_run(args: argparse.Namespace, config: object) -> int:
    _validate_run_options(args)
    if not _describe_run_plan(args).confirm(assume_yes=args.yes):
        print("Aborted.", flush=True)
        return 1
    started_at = datetime.now(UTC)
    suites = select_suites(discover_suites(TESTS_ROOT), group=args.test_group, case=args.case)
    if not suites:
        selector = f" and case {args.case!r}" if args.case else ""
        raise SystemExit(f"No suites found for group {args.test_group!r}{selector}.")

    # Every scenario reuses the same fixed DEFAULT_HOST_PORT regardless of
    # its own unique, timestamped Compose project name -- a second `run`
    # overlapping this one, or a stale container this one's own teardown
    # never reached (killed mid-run, crashed), collides on that port with a
    # raw, confusing Docker bind error instead of a clear "already running"
    # message. See se-lab's agent.common.run_lock() for the full rationale.
    with lab_common.run_lock(label="lab run"):
        if not args.skip_build:
            _checkout_source(args.target)
        values = _load_lab_env()
        if not args.skip_build:
            build_project = lab_common.project_name()
            _run_or_exit(values, build_project, "build", "family-librarian", "migrate")

        selector = args.case.lower() if args.case else args.test_group
        run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-suites-{selector}"
        run_directory = RESULTS_ROOT / run_id
        run_directory.mkdir(parents=True, exist_ok=False)

        # One factory, one run_suites() call, regardless of how many
        # different compose profiles the selected suites need between them
        # -- se-lab's dashboard/summary already tracks progress across an
        # arbitrary suite list in one continuous run (see m3undle_lab).
        # _scoped_for_run() is what points the shared factory at each
        # suite's own required profile(s) while that suite's own
        # setup/case/teardown is what's actually running.
        factory = _BaseScenarioFactory(values, keep=args.keep)
        summary = run_suites(
            _scoped_for_run(suites, factory), results_dir=run_directory, label="Family Librarian Lab", scenario_factory=factory
        )
        all_results = summary.results
        failed = summary.failed

        report = {
            "run_id": run_id,
            "profile": PROFILE,
            "group": args.test_group,
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

        completed_at = datetime.now(UTC)
        run_report = RunReport(label="Family Librarian Lab")
        if lab_common.is_git_checkout(lab_common.repo_dir()):
            branch = lab_common.repo_current_branch() or "detached"
            commit = lab_common.repo_head_commit(short=True) or "unknown"
            run_report.add("Source", f"branch {branch}")
            run_report.add("Lab commit", commit)
        run_report.add("Started at UTC", started_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        run_report.add("Completed at UTC", completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        run_report.add("Duration", lab_common.format_duration(int((completed_at - started_at).total_seconds())))
        run_report.add("Result", "FAIL" if failed else "PASS")
        run_report.print()

        return 1 if failed else 0
