"""Harness stamp: did the code that produced a result pass the selftest?

A stamp records the digest of the package sources at the moment the selftest
passed. A later run compares the current digest with the newest stamp:

* ``validated`` — a stamp exists and the digest matches;
* ``stale`` — code changed since the last passing selftest;
* ``not_run`` — no stamp at all.

The result never blocks a run; the CLI warns and the report shows a banner.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .. import VERSION
from .. import config

STAMP_DIR = Path("results") / "metadata-compare" / "selftest"
_CRLF = bytes([13, 10])
_LF = bytes([10])


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def harness_source_digest(root: Path | None = None) -> str:
    """sha256 over the sorted contents of ``metadata_lab/**/*.py``."""

    base = root or _package_root()
    hasher = hashlib.sha256()
    for path in sorted(base.rglob("*.py"), key=lambda item: item.as_posix()):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(base).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        # CRLF folds to LF so one commit has one digest on every platform checkout.
        hasher.update(path.read_bytes().replace(_CRLF, _LF))
        hasher.update(b"\0")
    return hasher.hexdigest()


def stamp_directory(root: Path | None = None) -> Path:
    base = Path(root) if root else config.repo_root()
    return base / STAMP_DIR


def stamp_path(digest: str, root: Path | None = None) -> Path:
    return stamp_directory(root) / f"{VERSION}-{digest[:12]}.json"


def write_stamp(
    *,
    passed: int,
    failed: int,
    duration_s: float,
    failures: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> Path:
    digest = harness_source_digest()
    now = datetime.now(timezone.utc)
    payload = {
        "schema": "metadata-lab.selftest/1",
        "tool_version": VERSION,
        "digest": digest,
        "selftest_id": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}",
        "ran_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": passed,
        "failed": failed,
        "duration_s": round(duration_s, 3),
        "failures": dict(failures or {}),
    }
    path = stamp_path(digest, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def latest_stamp(root: Path | None = None) -> dict[str, Any] | None:
    directory = stamp_directory(root)
    if not directory.is_dir():
        return None
    stamps = sorted(
        directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    if not stamps:
        return None
    try:
        return json.loads(stamps[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def harness_status(root: Path | None = None) -> dict[str, Any]:
    """Return the ``run.harness`` block for a new run."""

    current = harness_source_digest()
    stamp = latest_stamp(root)
    if stamp is None:
        return {"status": "not_run", "selftest_id": None, "digest": current}
    if stamp.get("digest") == current and not stamp.get("failed"):
        return {
            "status": "validated",
            "selftest_id": stamp.get("selftest_id"),
            "digest": current,
        }
    return {
        "status": "stale",
        "selftest_id": stamp.get("selftest_id"),
        "digest": current,
    }


__all__ = [
    "STAMP_DIR",
    "harness_source_digest",
    "harness_status",
    "latest_stamp",
    "stamp_directory",
    "stamp_path",
    "write_stamp",
]
