"""Save the request list before `lab clean-books` wipes it, and plan the requeue afterwards.

Only a book's identity (where it came from, who asked) and the formats asked for are kept. Request status is
used once, to pick formats for `--requeue-todo`, and is never restored: a requeued book starts a fresh request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = 1
SNAPSHOT_KEEP = 10

#: Per-format statuses that count as "to do". Available is done; Cancelled was dropped on purpose.
TODO_FORMAT_STATUSES = frozenset({"Requested", "NotAvailable"})


@dataclass(slots=True)
class SnapshotEntry:
    """One book wanted by one requester: where to find it again, and which formats (with status when saved)."""

    requester_email: str
    provider_id: str
    external_id: str
    title: str
    authors: list[str]
    formats: dict[str, str]  # media type -> status when saved
    note: str | None = None
    version_kind: str | None = None
    version_details: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "requesterEmail": self.requester_email,
            "providerId": self.provider_id,
            "externalId": self.external_id,
            "title": self.title,
            "authors": self.authors,
            "formats": self.formats,
            "note": self.note,
            "versionKind": self.version_kind,
            "versionDetails": self.version_details,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SnapshotEntry":
        return cls(
            requester_email=str(data["requesterEmail"]),
            provider_id=str(data["providerId"]),
            external_id=str(data["externalId"]),
            title=str(data.get("title") or ""),
            authors=[str(a) for a in data.get("authors") or []],
            formats={str(k): str(v) for k, v in (data.get("formats") or {}).items()},
            note=data.get("note"),
            version_kind=data.get("versionKind"),
            version_details=data.get("versionDetails"),
        )


@dataclass(slots=True)
class ReplayItem:
    entry: SnapshotEntry
    media_types: list[str]


@dataclass(slots=True)
class Unresolved:
    """A request that could not be saved, with the reason, so the summary can say what was left out."""

    title: str
    reason: str


@dataclass(slots=True)
class SnapshotBuild:
    entries: list[SnapshotEntry] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)


def build_entries(admin_requests: list[dict[str, Any]], work_sources: dict[str, tuple[str, str] | None]) -> SnapshotBuild:
    """Turn the admin request list into one entry per (requester, work), merging duplicate requests.

    `work_sources` maps a work id to the (provider id, external id) it can be resolved from again, or None when
    the work has no source. A format Available on any duplicate stays Available; otherwise Requested beats
    NotAvailable beats Cancelled, so a later live request is not hidden by an older cancelled one."""
    rank = {"Available": 3, "Requested": 2, "NotAvailable": 1, "Cancelled": 0}
    merged: dict[tuple[str, str], SnapshotEntry] = {}
    result = SnapshotBuild()
    reported: set[str] = set()
    for item in admin_requests:
        request = item.get("request") or {}
        work_id = str(request.get("workId", ""))
        title = str(request.get("workTitle") or work_id)
        source = work_sources.get(work_id)
        if source is None:
            if work_id not in reported:
                reported.add(work_id)
                result.unresolved.append(Unresolved(title, "the work has no provider source to resolve it from"))
            continue
        email = str(item.get("requesterEmail", ""))
        key = (email.lower(), work_id)
        entry = merged.get(key)
        if entry is None:
            entry = SnapshotEntry(
                requester_email=email,
                provider_id=source[0],
                external_id=source[1],
                title=title,
                authors=[str(a) for a in request.get("authors") or []],
                formats={},
                note=request.get("note"),
                version_kind=request.get("versionKind"),
                version_details=request.get("versionDetails"),
            )
            merged[key] = entry
            result.entries.append(entry)
        for fmt in request.get("formats") or []:
            media = str(fmt.get("mediaType", ""))
            status = str(fmt.get("status", ""))
            if media and rank.get(status, -1) >= rank.get(entry.formats.get(media, ""), -1):
                entry.formats[media] = status
    return result


def plan_replay(entries: list[SnapshotEntry], *, todo_only: bool) -> list[ReplayItem]:
    """Pick what to request again. `--requeue-all` takes every saved format, cancelled included; `--requeue-todo`
    takes only the formats that were not Available and not Cancelled, and drops books left with none."""
    plan: list[ReplayItem] = []
    for entry in entries:
        media = [m for m, status in entry.formats.items() if not todo_only or status in TODO_FORMAT_STATUSES]
        if media:
            plan.append(ReplayItem(entry, sorted(media)))
    return plan


def write_snapshot(directory: Path, entries: list[SnapshotEntry], *, now: datetime | None = None) -> Path | None:
    """Write a timestamped snapshot and prune to the newest SNAPSHOT_KEEP. Nothing is written for an empty list,
    so a reset of an already-empty lab cannot replace the snapshot a later `--requeue-*` still needs."""
    if not entries:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"requests-{stamp}.json"
    payload = {"version": SNAPSHOT_VERSION, "savedAtUtc": stamp, "entries": [e.to_json() for e in entries]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for old in sorted(directory.glob("requests-*.json"))[:-SNAPSHOT_KEEP]:
        old.unlink()
    return path


def latest_snapshot(directory: Path) -> Path | None:
    found = sorted(directory.glob("requests-*.json")) if directory.is_dir() else []
    return found[-1] if found else None


def read_snapshot(path: Path) -> list[SnapshotEntry]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"{path.name} has snapshot version {data.get('version')!r}; expected {SNAPSHOT_VERSION}.")
    return [SnapshotEntry.from_json(item) for item in data.get("entries", [])]
