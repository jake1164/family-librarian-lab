"""Command-line front door.

Every command goes through :mod:`metadata_lab.engine`, so the CLI, a batch suite
and the (later) UI cannot drift apart. Exit code 0 means "the run happened":
provider failures are data, not usage errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import config, registry
from .cases import CaseValidationError, Suite, load_suite_file, suite_from_result
from .compare import compare
from .engine import build_oneoff_suite, run_oneoff, run_suite
from .export import ai_export, full_export
from .http import HttpClient
from .model import Mode, Operation, ProviderError, RunMode
from .providers.base import ProviderAdapter
from .replay import ReplayHttp
from .report import write_report
from .selftest.stamp import harness_status

DEFAULT_OUT_ROOT = Path("results") / "metadata-compare"
EXIT_USAGE = 2


def _adapters() -> dict[str, type[ProviderAdapter]]:
    return registry.discover()


def _http_factory(adapters: dict[str, type[ProviderAdapter]]):
    def factory(provider_id: str) -> HttpClient:
        adapter_cls = adapters.get(provider_id)
        interval = float(getattr(adapter_cls, "min_interval_s", 1.0)) if adapter_cls else 1.0
        return HttpClient(provider_id=provider_id, min_interval_s=interval)

    return factory


def _split_providers(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _resolve_suite(target: str) -> Suite:
    candidate = Path(target)
    if candidate.is_file():
        return load_suite_file(candidate)
    if candidate.suffix == ".yaml" and not candidate.is_file():
        raise CaseValidationError(f"{target}: suite file not found")
    named = Path(__file__).resolve().parent / "suites" / f"{target}.yaml"
    if named.is_file():
        return load_suite_file(named)
    raise CaseValidationError(
        f"{target}: not a file and no suite named {target!r} in metadata_lab/suites/"
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _out_dir(value: str | None, label: str) -> Path:
    root = Path(value) if value else DEFAULT_OUT_ROOT
    directory = root / f"{_timestamp()}-{label}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_result(document: dict[str, Any], directory: Path) -> Path:
    path = directory / "result.json"
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _resolved_secrets(
    adapters: dict[str, type[ProviderAdapter]], document: dict[str, Any]
) -> set[str]:
    """Credential values actually used, for the export's leak assertion.

    The values never leave this function: ``full_export`` only compares them.
    """

    secrets: set[str] = set()
    for entry in document.get("providers", []):
        adapter_cls = adapters.get(str(entry.get("id")))
        if adapter_cls is None:
            continue
        value = config.resolve_credential(adapter_cls)
        if value:
            secrets.add(value)
    return secrets


def _write_outputs(
    document: dict[str, Any],
    directory: Path,
    adapters: dict[str, type[ProviderAdapter]],
) -> tuple[Path, Path, Path]:
    """Write result.json, ai.json and report.html for one run."""

    secrets = _resolved_secrets(adapters, document)
    full = full_export(document, secrets)
    result_path = _write_result(full, directory)
    ai_path = directory / "ai.json"
    ai_path.write_text(
        json.dumps(ai_export(full), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    report_path = write_report(full, directory / "report.html")
    return result_path, ai_path, report_path


def _print_summary(document: dict[str, Any]) -> None:
    run = document["run"]
    print(
        f"run {run['id']}  mode={run['mode']}  suite={document['suite']['name']} "
        f"v{document['suite']['version']}",
        flush=True,
    )
    for case in document["cases"]:
        note = "UNVERIFIED FIXTURE" if not case["verified"] else "reference verified"
        print(f"\ncase {case['id']}  ({note})", flush=True)
        for row in case["results"]:
            flags = []
            if row["truncated"]:
                flags.append("truncated")
            if row["error"]:
                flags.append(f"error={row['error']['kind']}")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(
                f"  {row['provider']:<14} {row['operation']:<16} {row['mode']:<12} "
                f"{row['outcome']:<24} {row['elapsed_ms']:>6} ms{suffix}",
                flush=True,
            )
            for check in row["checks"]:
                if check["outcome"] == "ok":
                    continue
                print(
                    f"      - {check['name']}: {check['outcome']} ({check['evidence']})",
                    flush=True,
                )


def _print_matrix(document: dict[str, Any]) -> None:
    print("\ncapability matrix (counts per outcome; no ranking)", flush=True)
    header = f"{'operation':<16}" + "".join(
        f"{provider['id']:<18}" for provider in document["providers"]
    )
    print(header, flush=True)
    for operation in Operation:
        cells = []
        for provider in document["providers"]:
            counts = document["matrix"].get(provider["id"], {}).get(operation.value)
            if not counts:
                cells.append("-")
                continue
            summary = ",".join(
                f"{name}={count}" for name, count in sorted(counts.items()) if name != "total"
            )
            cells.append(summary)
        print(f"{operation.value:<16}" + "".join(f"{cell:<18}" for cell in cells), flush=True)

    tallies = document.get("matrix_checks") or {}
    if not tallies:
        return
    print("\ncheck-level tallies (aspect: ok/total; other outcomes shown)", flush=True)
    for provider in document["providers"]:
        per_operation = tallies.get(provider["id"]) or {}
        for operation, aspects in per_operation.items():
            pieces = []
            for aspect, counts in sorted(aspects.items()):
                total = counts.get("total", 0)
                ok = counts.get("ok", 0)
                others = ",".join(
                    f"{name}={count}" for name, count in sorted(counts.items())
                    if name not in {"ok", "total"}
                )
                pieces.append(f"{aspect} {ok}/{total} ok" + (f" ({others})" if others else ""))
            print(f"  {provider['id']:<12} {operation:<16} " + "; ".join(pieces), flush=True)


# --- commands ---------------------------------------------------------------------


def _cmd_providers(args: argparse.Namespace) -> int:
    adapters = _adapters()
    payload: list[dict[str, Any]] = []
    for provider_id, adapter_cls in adapters.items():
        adapter = adapter_cls()
        entry = {
            "id": provider_id,
            "display_name": adapter.display_name,
            "adapter_version": adapter.adapter_version,
            "configured": config.is_configured(adapter_cls),
            "verified_live": bool(adapter.VERIFIED_LIVE),
            "credential_env": adapter.credential_env,
            "declared": {op.value: adapter.declared_support(op) for op in Operation},
        }
        payload.append(entry)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if not payload:
        print("no adapters registered (metadata_lab/providers/ is empty)")
        return 0
    for entry in payload:
        state = "configured" if entry["configured"] else "not configured"
        live = "verified live" if entry["verified_live"] else "not verified live"
        print(f"{entry['id']:<14} {entry['display_name']:<22} {state:<15} {live}")
        for operation, support in entry["declared"].items():
            print(f"    {operation:<16} {support}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    adapters = _adapters()
    selected = _split_providers(args.providers) or list(adapters)
    unknown = [provider for provider in selected if provider not in adapters]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE
    factory = _http_factory(adapters)
    for provider_id in selected:
        adapter_cls = adapters[provider_id]
        if not config.is_configured(adapter_cls):
            print(f"{provider_id}: not configured ({adapter_cls.credential_env} is empty)")
            continue
        ctx = factory(provider_id).context()
        try:
            text = adapter_cls().check_connection(ctx)
        except ProviderError as exc:
            print(f"{provider_id}: error - {exc.kind.value}: {exc.message}")
            continue
        except Exception as exc:  # never let a broken adapter crash the CLI
            print(f"{provider_id}: error - {type(exc).__name__}")
            continue
        print(f"{provider_id}: ok - {text}")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    adapters = _adapters()
    fields = {
        "title": args.title,
        "author": args.author,
        "isbn": args.isbn,
        "series": args.series,
    }
    try:
        document = run_oneoff(
            args.type,
            fields,
            _split_providers(args.providers),
            http_factory=_http_factory(adapters),
            adapters=adapters,
            as_of=args.as_of,
            harness=harness_status(),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    directory = _out_dir(args.out, f"query-{args.type}")
    result_path, ai_path, report_path = _write_outputs(document, directory, adapters)
    _print_summary(document)
    print(f"\nresult: {result_path}\nai: {ai_path}\nreport: {report_path}", flush=True)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    adapters = _adapters()
    try:
        suite = _resolve_suite(args.suite)
        if args.tags:
            wanted = {tag.strip() for tag in args.tags.split(",") if tag.strip()}
            cases = tuple(case for case in suite.cases if wanted & set(case.tags))
            if not cases:
                raise ValueError(f"no case in {suite.name!r} carries tag(s) {args.tags}")
            suite = replace(suite, cases=cases)
        resume = None
        if args.resume:
            try:
                resume = json.loads(Path(args.resume).read_text(encoding="utf-8"))
            except OSError as exc:
                print(f"{args.resume}: cannot read ({exc})", file=sys.stderr)
                return EXIT_USAGE
            except json.JSONDecodeError as exc:
                print(f"{args.resume}: not valid JSON ({exc})", file=sys.stderr)
                return EXIT_USAGE
        document = run_suite(
            suite,
            _split_providers(args.providers),
            [case.strip() for case in args.cases.split(",")] if args.cases else None,
            http_factory=_http_factory(adapters),
            adapters=adapters,
            traversal=args.traversal,
            exclude=_split_providers(args.exclude) or (),
            as_of=args.as_of,
            harness=harness_status(),
            resume=resume,
        )
    except (CaseValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    directory = _out_dir(args.out, suite.name)
    result_path, ai_path, report_path = _write_outputs(document, directory, adapters)
    _print_summary(document)
    _print_matrix(document)
    print(
        f"\nrun status: {document['run']['status']}  as_of: {document['run']['as_of']}  "
        f"harness: {document['run']['harness'].get('status')}",
        flush=True,
    )
    print(f"result: {result_path}\nai: {ai_path}\nreport: {report_path}", flush=True)
    if document["run"]["status"] == "cancelled":
        print("run cancelled; the partial result was written and can be resumed", file=sys.stderr)
        return 130
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    source = Path(args.result)
    try:
        original = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"{source}: cannot read ({exc})", file=sys.stderr)
        return EXIT_USAGE
    except json.JSONDecodeError as exc:
        print(f"{source}: not valid JSON ({exc})", file=sys.stderr)
        return EXIT_USAGE
    adapters = _adapters()
    try:
        suite = suite_from_result(original)
    except CaseValidationError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    replay = ReplayHttp.from_result(original)
    providers = [
        entry.get("id", "") for entry in original.get("providers", []) if entry.get("id")
    ]
    document = run_suite(
        suite,
        providers or None,
        None,
        http_factory=lambda provider_id: replay.for_provider(provider_id),
        adapters=adapters,
        run_mode=RunMode.REPLAY,
        replay_of=(original.get("run") or {}).get("id"),
        resolve_configured=lambda _cls: True,
    )
    directory = _out_dir(args.out, f"replay-{suite.name}")
    result_path, ai_path, report_path = _write_outputs(document, directory, adapters)
    _print_summary(document)
    print(f"\nresult: {result_path}\nai: {ai_path}\nreport: {report_path}", flush=True)
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest.runner import main as selftest_main

    argv: list[str] = []
    if args.only:
        argv += ["--only", args.only]
    if args.json:
        argv += ["--json"]
    return selftest_main(argv)


def _cmd_smoke(args: argparse.Namespace) -> int:
    """One polite live query per keyless provider. Never part of acceptance."""

    adapters = _adapters()
    selected = _split_providers(args.providers) or [
        provider_id
        for provider_id, adapter_cls in adapters.items()
        if not getattr(adapter_cls, "credential_env", None)
    ]
    print("smoke: live, non-gating; it does not validate the harness", flush=True)
    factory = _http_factory(adapters)
    for provider_id in selected:
        adapter_cls = adapters.get(provider_id)
        if adapter_cls is None:
            print(f"{provider_id}: unknown provider", file=sys.stderr)
            continue
        if not config.is_configured(adapter_cls):
            print(f"{provider_id}: skipped (not configured)")
            continue
        try:
            message = adapter_cls().check_connection(factory(provider_id).context())
        except ProviderError as exc:
            print(f"{provider_id}: error - {exc.kind.value}: {exc.message}")
        except Exception as exc:  # never let a smoke failure crash the CLI
            print(f"{provider_id}: error - {type(exc).__name__}")
        else:
            print(f"{provider_id}: ok - {message}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    documents = []
    for name in (args.first, args.second):
        try:
            documents.append(json.loads(Path(name).read_text(encoding="utf-8")))
        except OSError as exc:
            print(f"{name}: cannot read ({exc})", file=sys.stderr)
            return EXIT_USAGE
        except json.JSONDecodeError as exc:
            print(f"{name}: not valid JSON ({exc})", file=sys.stderr)
            return EXIT_USAGE
    result = compare(documents[0], documents[1])
    print(
        f"semantic view: {'identical' if result['semantic_equal'] else 'different'} "
        f"({len(result['semantic_changes'])} changed row(s))",
        flush=True,
    )
    if not result["changes"]:
        print("no provider, outcome or timing changes", flush=True)
    for change in result["changes"]:
        if change["kind"] in {"new_provider", "removed_provider"}:
            print(f"{change['kind']}: {change['provider']}", flush=True)
        elif change["kind"] == "reference_changed":
            print(f"reference_changed: case {change['case']}", flush=True)
        else:
            print(
                f"{change['kind']}: {change.get('case')} / {change.get('provider')} / "
                f"{change.get('operation')} "
                f"({change.get('before_outcome', change.get('before_ms'))} -> "
                f"{change.get('after_outcome', change.get('after_ms'))})",
                flush=True,
            )
    return 0


# --- parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metadata_lab",
        description="Read-only comparison of book metadata providers.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    providers = subparsers.add_parser("providers", help="list adapters and declared support")
    providers.add_argument("--json", action="store_true", help="machine-readable output")
    providers.set_defaults(func=_cmd_providers)

    check = subparsers.add_parser("check", help="connection test per provider")
    check.add_argument("--providers", help="comma-separated provider ids")
    check.set_defaults(func=_cmd_check)

    query = subparsers.add_parser("query", help="one-off query")
    query.add_argument("--type", required=True, choices=["book", "isbn", "author", "series"])
    query.add_argument("--title")
    query.add_argument("--author")
    query.add_argument("--isbn")
    query.add_argument("--series")
    query.add_argument("--providers", help="comma-separated provider ids")
    query.add_argument("--as-of", dest="as_of", help="evaluation date (YYYY-MM-DD)")
    query.add_argument("--out", help=f"output root (default {DEFAULT_OUT_ROOT})")
    query.set_defaults(func=_cmd_query)

    run = subparsers.add_parser("run", help="run a suite file or a named suite")
    run.add_argument("suite", help="path to a .yaml suite, or a name in metadata_lab/suites/")
    run.add_argument("--providers", help="comma-separated provider ids")
    run.add_argument("--cases", help="comma-separated case ids")
    run.add_argument("--exclude", help="comma-separated provider ids to mark not_run (excluded)")
    run.add_argument("--tags", help="comma-separated tags; only matching cases run")
    run.add_argument("--as-of", dest="as_of", help="evaluation date (YYYY-MM-DD)")
    run.add_argument("--resume", help="previous result.json to continue")
    run.add_argument(
        "--traversal",
        action="store_true",
        default=None,
        help="also run the traversal measurement for every case",
    )
    run.add_argument("--out", help=f"output root (default {DEFAULT_OUT_ROOT})")
    run.set_defaults(func=_cmd_run)

    replay = subparsers.add_parser("replay", help="re-run a saved result from its raw captures")
    replay.add_argument("result", help="path to a previous result.json")
    replay.add_argument("--out", help=f"output root (default {DEFAULT_OUT_ROOT})")
    replay.set_defaults(func=_cmd_replay)

    compare = subparsers.add_parser("compare", help="diff two result files")
    compare.add_argument("first")
    compare.add_argument("second")
    compare.set_defaults(func=_cmd_compare)

    selftest = subparsers.add_parser("selftest", help="harness validation suite (H01-H24)")
    selftest.add_argument("--only", help="comma-separated scenario ids")
    selftest.add_argument("--json", action="store_true", help="machine-readable summary")
    selftest.set_defaults(func=_cmd_selftest)

    smoke = subparsers.add_parser(
        "smoke", help="one polite live query per keyless provider (never part of acceptance)"
    )
    smoke.add_argument("--providers", help="comma-separated provider ids")
    smoke.set_defaults(func=_cmd_smoke)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


__all__ = ["build_parser", "main"]
