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
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from agent import registry


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "compose.base.yaml"
LAB_ENV_FILE = REPO_ROOT / "lab.env"
RESULTS_ROOT = REPO_ROOT / "results"
PROFILE = "base"
DEFAULT_HOST_PORT = 18080
_SECRET_NAMES = (
    "FAMILY_LIBRARIAN_POSTGRES_PASSWORD",
    "FAMILY_LIBRARIAN_ADMIN_PASSWORD",
)


def _configure_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fresh", action="store_true", help="Remove this run's Compose volumes before starting")
    parser.add_argument("--project-name", default=None, help="Compose project name (default: a unique run name)")


def _configure_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-name", default=None, help="Compose project name (default: FAMILY_LIBRARIAN_PROJECT_NAME)")


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
    return values


def _project_name(values: dict[str, str], requested: str | None, *, unique: bool) -> str:
    candidate = requested or values.get("FAMILY_LIBRARIAN_PROJECT_NAME")
    if not candidate and unique:
        candidate = "family-librarian-lab-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    if not candidate:
        raise SystemExit("Specify --project-name or set FAMILY_LIBRARIAN_PROJECT_NAME in lab.env.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", candidate):
        raise SystemExit("Compose project names may contain lowercase letters, digits, hyphens, and underscores only.")
    return candidate


def _compose(values: dict[str, str], project_name: str, *arguments: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
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
        "--profile",
        PROFILE,
        *arguments,
    ]
    return subprocess.run(command, env=environment, text=True, capture_output=capture, check=False)


def _run_or_exit(values: dict[str, str], project_name: str, *arguments: str) -> None:
    result = _compose(values, project_name, *arguments)
    if result.returncode:
        raise SystemExit(result.returncode)


def _host_base(values: dict[str, str]) -> str:
    port = values.get("FAMILY_LIBRARIAN_HOST_PORT", str(DEFAULT_HOST_PORT))
    try:
        numeric_port = int(port)
    except ValueError as error:
        raise SystemExit("FAMILY_LIBRARIAN_HOST_PORT must be a valid TCP port.") from error
    if not 1 <= numeric_port <= 65535:
        raise SystemExit("FAMILY_LIBRARIAN_HOST_PORT must be between 1 and 65535.")
    return f"http://127.0.0.1:{numeric_port}"


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

    ps = _compose(values, project_name, "ps", "--all", "--format", "json", capture=True)
    config = _compose(values, project_name, "config", "--no-interpolate", capture=True)
    logs = _compose(values, project_name, "logs", "--no-color", capture=True)

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
    result = _compose(values, project_name, "ps", "--all", "--format", "json", capture=True)
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
    base = _host_base(values)
    checks: dict[str, object] = {
        "live": _probe(f"{base}/health/live"),
        "ready": _probe(f"{base}/health/ready"),
    }
    services, services_ready = _compose_service_health(values, project_name)
    checks["compose_services"] = services
    passed = (
        all(bool(item.get("ok")) for name, item in checks.items() if name != "compose_services" and isinstance(item, dict))
        and services_ready
    )
    return checks, passed


@registry.command("build", help="Build the Family Librarian base-profile image")
def handle_build(args: argparse.Namespace, config: object) -> int:
    values = _load_lab_env()
    project_name = _project_name(values, None, unique=False)
    _run_or_exit(values, project_name, "build", "family-librarian", "migrate")
    return 0


@registry.command("run", help="Start the isolated base profile and record readiness", configure=_configure_run)
def handle_run(args: argparse.Namespace, config: object) -> int:
    values = _load_lab_env()
    project_name = _project_name(values, args.project_name, unique=True)
    if args.fresh:
        _run_or_exit(values, project_name, "down", "--volumes", "--remove-orphans")
    _run_or_exit(values, project_name, "up", "--build", "--wait", "--remove-orphans")
    checks, passed = _readiness(values, project_name)
    result_path = _capture_result(values, project_name, checks, "pass" if passed else "fail")
    print(f"Base result captured: {result_path}", flush=True)
    if not passed:
        print(json.dumps(checks, indent=2), file=sys.stderr)
        return 1
    print(f"Base profile ready: {project_name}", flush=True)
    return 0


@registry.command("status", help="Report base-profile Compose and HTTP health", configure=_configure_project)
def handle_status(args: argparse.Namespace, config: object) -> int:
    values = _load_lab_env()
    project_name = _project_name(values, args.project_name, unique=False)
    ps = _compose(values, project_name, "ps", "--all", "--format", "json", capture=True)
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
    _run_or_exit(values, project_name, "down", "--remove-orphans")
    print(f"Base profile stopped: {project_name}", flush=True)
    return 0
