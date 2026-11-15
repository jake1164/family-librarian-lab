"""The work side of the local UI: one job at a time, progress, cancel, self-test.

A job runs on its own thread; the browser only ever sees a job id and a progress
dict. ``should_cancel`` is handed to the engine, so a cancel reuses the one
cancel path (the same code as Ctrl-C) and still writes a partial result.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import artifacts, config
from .engine import run_suite
from .http import HttpClient, scrub
from .model import ProviderError
from .pacing import CooldownStore
from .providers.base import ProviderAdapter
from .selftest.runner import run_all
from .selftest.stamp import harness_status, latest_stamp, write_stamp
from .serve_data import RunRequest

AdapterMap = Mapping[str, type[ProviderAdapter]]


class Busy(RuntimeError):
    """A job is already running; the server answers 409 instead of queueing."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Job:
    """One run, its progress and its files."""

    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "queued"
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.progress: dict[str, Any] = {
            "rows_done": 0,
            "rows_total": 0,
            "calls": 0,
            "case": None,
            "provider": None,
            "operation": None,
            "mode": None,
        }
        self.error: str | None = None
        self.run_dir: str | None = None
        self.run_id: str | None = None
        self.suite: str | None = None
        self.files: dict[str, str] | None = None
        self._cancel = threading.Event()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self) -> None:
        self._cancel.set()

    def on_progress(self, payload: Mapping[str, Any]) -> None:
        for key in ("rows_done", "rows_total", "calls", "case", "provider", "operation", "mode"):
            if key in payload:
                self.progress[key] = payload[key]

    def finish(self, document: Mapping[str, Any], directory: Path, paths: tuple[Path, ...]) -> None:
        run = document.get("run") or {}
        self.status = "cancelled" if run.get("status") == "cancelled" else "complete"
        self.run_id = run.get("id")
        self.run_dir = directory.name
        self.finished_at = _now()
        self.files = {path.name: f"/api/runs/{directory.name}/{path.name}" for path in paths}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": dict(self.progress),
            "cancel_requested": self.cancelled(),
            "error": self.error,
            "suite": self.suite,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "files": self.files,
        }


class JobManager:
    """Runs the engine for the UI. The lock is the "one run at a time" rule."""

    def __init__(
        self,
        *,
        adapters: AdapterMap,
        http_factory: Callable[[str], HttpClient],
        results_root: Path,
        stamp_root: Path | None = None,
        cooldowns: CooldownStore | None = None,
    ) -> None:
        self.adapters = dict(adapters)
        self.http_factory = http_factory
        self.results_root = Path(results_root)
        self.stamp_root = stamp_root
        self.cooldowns = cooldowns
        self._gate = threading.Lock()
        self._jobs: dict[str, Job] = {}

    # -- jobs ------------------------------------------------------------------

    def submit(self, request: RunRequest) -> Job:
        self._claim()
        job = Job("run")
        job.suite = request.suite.name
        self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run, args=(job, request), name=f"metadata-lab-{job.id}", daemon=True
        )
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in {"queued", "running"}:
            job.request_cancel()
        return job

    def _run(self, job: Job, request: RunRequest) -> None:
        job.status = "running"
        job.started_at = _now()
        try:
            document = run_suite(
                request.suite,
                list(request.providers),
                None,
                http_factory=self.http_factory,
                adapters=self.adapters,
                traversal=request.traversal,
                as_of=request.as_of,
                harness=harness_status(self.stamp_root),
                should_cancel=job.cancelled,
                progress=job.on_progress,
                cooldowns=self.cooldowns,
                ignore_cooldown=request.ignore_cooldown,
            )
            directory = artifacts.run_directory(self.results_root, request.suite.name)
            paths = artifacts.write_outputs(document, directory, self.adapters)
            job.finish(document, directory, paths)
        except BaseException as exc:  # a worker must always release the gate
            job.status = "error"
            job.error = self.redact(f"{type(exc).__name__}: {exc}")
            job.finished_at = _now()
        finally:
            self._gate.release()

    # -- one-off work that is not a run ----------------------------------------

    def run_exclusive(self, work: Callable[[], Any]) -> Any:
        """Run ``work`` while holding the gate (connection checks, self-test)."""

        self._claim()
        try:
            return work()
        finally:
            self._gate.release()

    def check(self, provider_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        """One cheap call per provider, under the gate: never a credential value."""

        return self.run_exclusive(lambda: self.check_providers(provider_ids))

    def check_providers(self, provider_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        """One cheap call per provider. Never returns a credential value."""

        results: list[dict[str, Any]] = []
        for provider_id in provider_ids:
            adapter_cls = self.adapters[provider_id]
            if not config.is_configured(adapter_cls):
                results.append(
                    {
                        "provider": provider_id,
                        "status": "not_configured",
                        "message": f"{adapter_cls.credential_env} is not set on the server",
                    }
                )
                continue
            secrets = {value for value in [config.resolve_credential(adapter_cls)] if value}
            try:
                message = adapter_cls().check_connection(self.http_factory(provider_id).context())
            except ProviderError as exc:
                results.append(
                    {
                        "provider": provider_id,
                        "status": "error",
                        "kind": exc.kind.value,
                        "message": str(scrub(exc.message, secrets)),
                    }
                )
            except Exception as exc:  # never let a broken adapter reach the browser
                results.append(
                    {
                        "provider": provider_id,
                        "status": "error",
                        "kind": "parse",
                        "message": f"adapter raised {type(exc).__name__}",
                    }
                )
            else:
                results.append(
                    {
                        "provider": provider_id,
                        "status": "ok",
                        "message": str(scrub(message, secrets)),
                    }
                )
        return results

    def selftest(self) -> dict[str, Any]:
        """Run the full offline self-test (about 3 s) and stamp it."""

        def work() -> dict[str, Any]:
            summary = run_all()
            stamp = write_stamp(
                passed=summary["passed"],
                failed=summary["failed"],
                duration_s=summary["duration_s"],
                failures=summary["failures"],
                root=self.stamp_root,
            )
            return {
                "results": summary["results"],
                "passed": summary["passed"],
                "failed": summary["failed"],
                "duration_s": summary["duration_s"],
                "stamp": str(stamp),
                "harness": harness_status(self.stamp_root),
            }

        return self.run_exclusive(work)

    # -- helpers ---------------------------------------------------------------

    def _claim(self) -> None:
        if not self._gate.acquire(blocking=False):
            raise Busy("a run or self-test is already in progress")

    def redact(self, text: str) -> str:
        """Strip any credential value that a message might have picked up."""

        secrets: set[str] = set()
        for adapter_cls in self.adapters.values():
            value = config.resolve_credential(adapter_cls)
            if value:
                secrets.add(value)
        return str(scrub(text, secrets))


def harness_payload(stamp_root: Path | None) -> dict[str, Any]:
    """The header badge: is the harness that would produce a run validated?"""

    stamp = latest_stamp(stamp_root) or {}
    status = harness_status(stamp_root)
    return {
        **status,
        "ran_at": stamp.get("ran_at"),
        "passed": stamp.get("passed"),
        "failed": stamp.get("failed"),
        "duration_s": stamp.get("duration_s"),
    }


__all__ = ["Busy", "Job", "JobManager", "harness_payload"]
