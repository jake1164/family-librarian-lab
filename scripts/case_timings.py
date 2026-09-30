"""Per-case timing from a lab results directory.

    python scripts/case_timings.py [results-dir] [--since 20260930T00]

For each scenario directory, prints how long the stack took to become ready
(start of the project name -> readiness capture) and how long the case plus
teardown took (until the next scenario started). Use it before and after a
change to Compose, healthchecks or teardown so a speed-up is measured, not
assumed. Scenarios run one at a time, so "until the next start" is the case's
wall-clock share of the run.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

_DIR = re.compile(r"(\d{8}T\d{6})Z-family-librarian-lab-([a-z0-9-]+)-(\d{20})$")


def _rows(results: Path, since: str) -> list[tuple[datetime, datetime, str]]:
    rows = []
    for entry in results.iterdir():
        match = _DIR.match(entry.name)
        if not match or not entry.name.startswith(since):
            continue
        ready = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
        started = datetime.strptime(match.group(3)[:14], "%Y%m%d%H%M%S")
        rows.append((started, ready, match.group(2)))
    return sorted(rows)


def main(argv: list[str]) -> int:
    since = ""
    args = list(argv)
    if "--since" in args:
        index = args.index("--since")
        since = args[index + 1]
        del args[index : index + 2]
    results = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "results"
    rows = _rows(results, since)
    if not rows:
        print("no scenario directories found")
        return 1
    ready_total = case_total = 0.0
    for index, (started, ready, case) in enumerate(rows):
        to_ready = (ready - started).total_seconds()
        # The last row has no successor, so its case time is unknown.
        case_time = (rows[index + 1][0] - started).total_seconds() if index + 1 < len(rows) else None
        ready_total += to_ready
        case_total += case_time or 0.0
        shown = f"{case_time:6.0f}s" if case_time is not None else "   n/a"
        print(f"{case:14s} ready {to_ready:4.0f}s   case+teardown {shown}")
    print(f"{len(rows)} cases: {ready_total:.0f}s starting stacks, {case_total:.0f}s total wall-clock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
