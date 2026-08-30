"""Backup/restore lifecycle, exercised against a real base deployment.

scripts/backups/create-backup.sh and restore-backup.sh are host-operator
scripts, not something the running app's HTTP API exposes -- they shell out
directly to `docker compose exec postgres pg_dump`/`pg_restore`. This suite
runs them completely unmodified (see `_BaseScenario.run_backup_script()` in
commands.py) against this scenario's own real, disposable Compose project,
the same way an operator would run them against a real deployment, rather
than re-testing their internal argument/validation logic -- that is already
covered directly, with a stubbed and a real-Postgres case each, by
family-librarian's own tests/scripts/*.test.sh.

CWA/Audiobookshelf directory backup is scripts/backups' own concern (also
covered there) -- this case leaves both disabled in its backup config. A
real CWA destination still has to come up, same as base-security/
base-restart (`extra_profiles=(clients.CWA_PROFILE,)`): BookRequestService's
FormatReadinessService gate rejects creating any request at all, HTTP 400,
unless a ready destination exists for that media type -- see
test_base_security.py's module docstring. Unlike base-restart, CWA is left
running throughout; this case only needs requests creatable, not CWA itself
unavailable.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

from agent.suites import suite

from family_librarian_lab import clients
from family_librarian_lab.commands import ensure_shared_clamav, teardown_shared_clamav

SUITE = suite("backup-restore", group="base", order=12, extra_profiles=(clients.CWA_PROFILE,))


@SUITE.setup
def _setup(scenario_factory):
    scenario_factory.extra_env = ensure_shared_clamav()


@SUITE.teardown
def _teardown():
    teardown_shared_clamav()


def _run(ctx, test_id: str, operation: Callable[[], dict[str, object]]) -> None:
    try:
        detail = operation()
    except AssertionError as error:
        ctx.fail(test_id, str(error))
    except Exception as error:  # suite runner keeps later scenarios independent
        ctx.fail(test_id, f"Scenario failed unexpectedly: {error}")
    else:
        ctx.ok(test_id, "Scenario assertions passed.", detail)


@SUITE.case("BASE-04")
def backup_then_restore_reverts_to_the_backed_up_state(ctx, scenario_factory):
    def operation() -> dict[str, object]:
        with scenario_factory("BASE-04") as scenario:
            baseline_request_id, _ = scenario.api.create_demo_ebook_request()
            if not _has_request(scenario.api.list_requests(), baseline_request_id):
                raise AssertionError("Baseline request was not visible before backup.")

            with tempfile.TemporaryDirectory(prefix="family-librarian-lab-backup-") as work_dir:
                work = Path(work_dir)
                config_path = work / "backup.env"
                config_path.write_text(
                    "\n".join(
                        [
                            f"BACKUP_OUTPUT_DIRECTORY={work / 'backups'}",
                            "CWA_BACKUP_MODE=disabled",
                            "AUDIOBOOKSHELF_BACKUP_MODE=disabled",
                            "DEPLOYMENT_SECRET_RECOVERY_REFERENCE=family-librarian-lab-BASE-04",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )

                backup_directory = scenario.run_backup_script("create-backup.sh", "--config", str(config_path)).strip()
                scenario.run_backup_script("verify-backup.sh", "--backup", backup_directory)

                # Created only after the backup: proves restore reverts to
                # the backed-up snapshot rather than merely that pg_restore
                # ran without error over already-matching data.
                post_backup_request_id, _ = scenario.api.create_demo_ebook_request()
                if not _has_request(scenario.api.list_requests(), post_backup_request_id):
                    raise AssertionError("Post-backup request was not visible before restore.")

                scenario.run_backup_script("restore-backup.sh", "--backup", backup_directory, "--confirm-replace-postgres")

            # restore-backup.sh brings family-librarian back up without
            # --wait; reuse the scenario's own readiness wait rather than a
            # bespoke poll, then refresh the session the host restart ended
            # -- same pattern BASE-03 uses after its own restart_service().
            scenario.start_service("family-librarian")
            scenario.reauthenticate()

            restored_ids = {_request_id(entry) for entry in scenario.api.list_requests()}
            if baseline_request_id not in restored_ids:
                raise AssertionError("Restore did not bring back the pre-backup request.")
            if post_backup_request_id in restored_ids:
                raise AssertionError("Restore left the post-backup request in place; PostgreSQL was not actually replaced.")

            return {
                "compose_project": scenario.project_name,
                "backup_directory": backup_directory,
                "baseline_request_id": baseline_request_id,
                "post_backup_request_id": post_backup_request_id,
            }

    _run(ctx, "BASE-04", operation)


def _request_id(entry: dict[str, Any]) -> object:
    request = entry.get("request")
    return request.get("id") if isinstance(request, dict) else None


def _has_request(requests: list[dict[str, Any]], request_id: str) -> bool:
    return any(_request_id(entry) == request_id for entry in requests)
