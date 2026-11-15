"""Where a run's files go, and the one way to write them.

The CLI and the local UI both produce exactly three files per run
(``result.json``, ``ai.json``, ``report.html``). Keeping that in one place is why
the UI can reuse the CLI's output without a second renderer.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import config
from .export import ai_export, full_export
from .report import write_report

DEFAULT_RESULTS = Path("results") / "metadata-compare"
_UNSAFE = re.compile(r"[^0-9A-Za-z._-]+")


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_directory(root: Path | str, label: str) -> Path:
    """Create ``<root>/<UTC timestamp>-<label>`` and return it.

    ``label`` is sanitised because the directory name is served back as a run id,
    and the name gets a counter rather than clashing when two runs start in the
    same second (the UI runs one at a time, the CLI does not).
    """

    safe = _UNSAFE.sub("-", label).strip("-") or "run"
    base = Path(root) / f"{timestamp()}-{safe}"
    directory = base
    counter = 1
    while directory.exists():
        counter += 1
        directory = Path(f"{base}-{counter}")
    directory.mkdir(parents=True)
    return directory


def resolved_secrets(
    adapters: Mapping[str, type], document: Mapping[str, Any]
) -> set[str]:
    """Credential values actually used, for the export's leak assertion.

    The values never leave this function: ``full_export`` only compares them.
    """

    secrets: set[str] = set()
    for entry in document.get("providers", []) or []:
        adapter_cls = adapters.get(str(entry.get("id")))
        if adapter_cls is None:
            continue
        value = config.resolve_credential(adapter_cls)
        if value:
            secrets.add(value)
    return secrets


def write_outputs(
    document: dict[str, Any], directory: Path, adapters: Mapping[str, type]
) -> tuple[Path, Path, Path]:
    """Write result.json, ai.json and report.html for one run."""

    full = full_export(document, resolved_secrets(adapters, document))
    result_path = directory / "result.json"
    result_path.write_text(json.dumps(full, indent=2, ensure_ascii=False), encoding="utf-8")
    ai_path = directory / "ai.json"
    ai_path.write_text(
        json.dumps(ai_export(full), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    report_path = write_report(full, directory / "report.html")
    return result_path, ai_path, report_path


__all__ = [
    "DEFAULT_RESULTS",
    "resolved_secrets",
    "run_directory",
    "timestamp",
    "write_outputs",
]
