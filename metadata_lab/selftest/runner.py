"""Selftest runner: offline guard, fake clock, PASS/FAIL reporting."""

from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import config
from ..cases import Case, Reference, Suite
from ..engine import run_oneoff, run_suite
from ..export import ai_export, full_export
from ..http import HttpClient, Transport
from ..model import Mode, Operation
from ..report import write_report
from .server import ScriptedServer

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@dataclass
class Evidence:
    ok: bool
    detail: str = ""


@dataclass
class Scenario:
    id: str
    difficulty: str
    title: str
    run: Callable[["Harness"], Evidence]


class FakeClock:
    """A virtual clock so retries cost no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class OfflineGuard:
    """Fail any connection attempt to a non-loopback address (H23)."""

    def __init__(self) -> None:
        self._original = None
        self.blocked: list[str] = []

    def __enter__(self) -> "OfflineGuard":
        original = socket.socket.connect

        def guarded(self_socket, address, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) else str(address)
            if host not in LOOPBACK:
                raise RuntimeError(f"selftest blocked a non-loopback connection to {host}")
            return original(self_socket, address, *args, **kwargs)

        self._original = original
        socket.socket.connect = guarded  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._original is not None:
            socket.socket.connect = self._original  # type: ignore[assignment]


class Harness:
    """Everything a scenario may use: a scripted server, virtual time, temp files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.server = ScriptedServer()
        self.clock = FakeClock()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.transports: dict[str, Transport] = {}
        self.adapters: dict[str, type] = {}

    # -- helpers -----------------------------------------------------------

    def http_factory(self, adapters: Mapping[str, type]):
        def factory(provider_id: str) -> HttpClient:
            adapter_cls = adapters.get(provider_id)
            interval = float(getattr(adapter_cls, "min_interval_s", 0.0) or 0.0)
            return HttpClient(
                provider_id=provider_id,
                transport=self.transports.get(provider_id),
                min_interval_s=interval,
                sleep=self.clock.sleep,
                clock=self.clock.clock,
            )

        return factory

    def run(
        self,
        adapters: Mapping[str, type],
        *,
        operations: Sequence[Operation] = (Operation.SERIES_MEMBERS,),
        case_input: Mapping[str, str] | None = None,
        expect: Mapping[str, Any] | None = None,
        providers: Sequence[str] | None = None,
        cases: Sequence[Case] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if cases is None:
            case = Case(
                id="selftest-case",
                description="selftest",
                reference=Reference(source="selftest", verified_on=None),
                input=dict(
                    case_input
                    or {"title": "Fourth Wing", "author": "Rebecca Yarros", "series": "The Empyrean"}
                ),
                operations=tuple(operations),
                traversal=False,
                options=dict(kwargs.pop("options", {})),
                expect=dict(expect or {}),
                verified=False,
            )
            cases = (case,)
        suite = Suite(
            name=kwargs.pop("suite_name", "selftest"),
            version=1,
            cases=tuple(cases),
            source="selftest",
            sha256="0" * 64,
            as_of=kwargs.pop("suite_as_of", None),
        )
        return run_suite(
            suite,
            providers,
            kwargs.pop("case_ids", None),
            http_factory=self.http_factory(adapters),
            adapters=dict(adapters),
            **kwargs,
        )

    def oneoff(self, kind: str, fields: Mapping[str, Any], adapters: Mapping[str, type]):
        return run_oneoff(
            kind,
            fields,
            list(adapters),
            http_factory=self.http_factory(adapters),
            adapters=dict(adapters),
        )

    def write_outputs(self, document: Mapping[str, Any], name: str = "run") -> dict[str, Path]:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        result = directory / "result.json"
        result.write_text(json.dumps(full_export(document), indent=2), encoding="utf-8")
        ai = directory / "ai.json"
        ai.write_text(json.dumps(ai_export(document), indent=2), encoding="utf-8")
        report = write_report(document, directory / "report.html")
        return {"result": result, "ai": ai, "report": report}


SCENARIOS: list[Scenario] = []


def scenario(scenario_id: str, difficulty: str, title: str):
    def register(function: Callable[[Harness], Evidence]) -> Callable[[Harness], Evidence]:
        SCENARIOS.append(Scenario(scenario_id, difficulty, title, function))
        return function

    return register


def run_all(only: Sequence[str] | None = None) -> dict[str, Any]:
    """Run every scenario (or the listed ids) and return a summary."""

    from . import scenarios  # noqa: F401  (registers the scenarios)

    selected = [
        item for item in sorted(SCENARIOS, key=lambda scenario: scenario.id)
        if not only or item.id in set(only)
    ]
    results: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="metadata-lab-selftest-") as tmp:
        harness = Harness(Path(tmp))
        harness.server.start()
        try:
            with OfflineGuard():
                for item in selected:
                    harness.server.reset()
                    harness.clock = FakeClock()
                    harness.transports.clear()
                    try:
                        evidence = item.run(harness)
                    except Exception as exc:  # a crashing scenario is a failure, not a crash
                        evidence = Evidence(False, f"{type(exc).__name__}: {exc}")
                    results.append(
                        {
                            "id": item.id,
                            "difficulty": item.difficulty,
                            "title": item.title,
                            "ok": evidence.ok,
                            "detail": evidence.detail,
                        }
                    )
                    if not evidence.ok:
                        failures[item.id] = evidence.detail
        finally:
            harness.server.stop()
    duration = time.monotonic() - started
    return {
        "results": results,
        "passed": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "duration_s": round(duration, 2),
        "failures": failures,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m metadata_lab selftest`` entry point (used by the CLI)."""

    args = list(argv or [])
    only: list[str] = []
    as_json = "--json" in args
    if "--only" in args:
        index = args.index("--only")
        if index + 1 < len(args):
            only = [item.strip() for item in args[index + 1].split(",") if item.strip()]
    summary = run_all(only or None)
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        for item in summary["results"]:
            flag = "PASS" if item["ok"] else "FAIL"
            print(f"{flag} {item['id']} [{item['difficulty']}] {item['title']}")
            if not item["ok"]:
                print(f"     {item['detail']}")
        print(
            f"\n{summary['passed']}/{len(summary['results'])} passed in "
            f"{summary['duration_s']}s"
        )
    if summary["failed"]:
        return 1
    if only:
        # A filtered run is a debugging aid, never an acceptance stamp.
        if not as_json:
            print("partial run: no stamp written (run the full suite to stamp)")
        return 0
    from .stamp import write_stamp

    stamp = write_stamp(
        passed=summary["passed"],
        failed=summary["failed"],
        duration_s=summary["duration_s"],
        failures=summary["failures"],
    )
    if not as_json:
        print(f"stamp: {stamp}")
    return 0


__all__ = [
    "Evidence",
    "FakeClock",
    "Harness",
    "OfflineGuard",
    "SCENARIOS",
    "Scenario",
    "main",
    "run_all",
    "scenario",
]
