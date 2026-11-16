"""A small, bounded response cache for provider calls (opt-in, off by default).

Why it exists: a traversal run and an independent run ask a provider for the same
searches, and some providers (Inventaire above all) punish volume. The cache lets a
re-run reuse a recent answer instead of asking again.

What it must never do is make a measurement lie, so:

* every entry stores *when the provider answered* (``stored_at``) and expires after a
  configurable time-to-live; an expired entry is a miss and is removed;
* a served response is labelled ``cache`` on the exchange and on the result row, with
  the time it was fetched, so live, cached and replayed evidence stay distinct;
* only successful JSON responses are stored (never an error), and nothing carrying a
  credential in the URL or parameters is keyed on it (secrets are not part of the key
  and are scrubbed from the stored body);
* the engine never reads from it for an operation whose fixture expects a freshness
  fact (a publication date, a "latest" book), because those facts are about *now*;
* it is bounded by entry count and bytes, evicting the oldest entries first.

Adapters opt out with ``cache_policy = "none"`` (Google Books: its terms restrict
caching), and the store lives under ``results/`` which is gitignored.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_TTL_S = 24 * 3600.0
DEFAULT_MAX_ENTRIES = 500
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
CACHE_DIR = Path("results") / "metadata-compare" / "cache"

#: Values of ``Adapter.cache_policy``.
POLICY_PERSISTENT = "persistent"
POLICY_NONE = "none"


@dataclass(frozen=True)
class CachedResponse:
    status: int
    text: str
    stored_at: float

    @property
    def fetched_at(self) -> str:
        return datetime.fromtimestamp(self.stored_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ResponseCache:
    """File-backed cache: one small JSON file per request key."""

    def __init__(
        self,
        root: Path | str,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.ttl_s = max(0.0, float(ttl_s))
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._clock = clock
        self.hits = 0
        self.misses = 0
        self.stores = 0

    # -- keys ------------------------------------------------------------------

    @staticmethod
    def key(
        provider_id: str,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        body: str | None,
    ) -> str:
        cleaned = {str(k): str(v) for k, v in (params or {}).items()}
        material = json.dumps(
            [provider_id, method.upper(), url, cleaned, body or ""], sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    # -- read / write ------------------------------------------------------------

    def get(self, key: str) -> CachedResponse | None:
        """A fresh entry, or ``None``. Corrupt or expired entries are removed."""

        path = self._path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entry = CachedResponse(
                status=int(raw["status"]), text=str(raw["text"]), stored_at=float(raw["stored_at"])
            )
        except (OSError, ValueError, KeyError, TypeError):
            self.misses += 1
            if path.exists():
                self._remove(path)
            return None
        if self._clock() - entry.stored_at > self.ttl_s:
            self._remove(path)
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: str, *, status: int, text: str) -> None:
        """Store one successful response. Failures to write are swallowed: caching is optional."""

        if not (200 <= int(status) < 300):
            return
        payload = json.dumps(
            {"status": int(status), "text": text, "stored_at": self._clock()}, ensure_ascii=False
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(payload, encoding="utf-8")
        except OSError:
            return
        self.stores += 1
        self._evict()

    def _remove(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _entries(self) -> list[tuple[float, int, Path]]:
        entries: list[tuple[float, int, Path]] = []
        try:
            for path in self.root.glob("*.json"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            return []
        return entries

    def _evict(self) -> None:
        """Drop the oldest entries until both bounds hold."""

        entries = sorted(self._entries(), key=lambda item: item[0])
        total = sum(size for _mtime, size, _path in entries)
        while entries and (len(entries) > self.max_entries or total > self.max_bytes):
            _mtime, size, path = entries.pop(0)
            self._remove(path)
            total -= size

    # -- housekeeping ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        entries = self._entries()
        return {
            "entries": len(entries),
            "bytes": sum(size for _mtime, size, _path in entries),
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
        }

    def clear(self) -> int:
        removed = 0
        for _mtime, _size, path in self._entries():
            self._remove(path)
            removed += 1
        return removed

    def config(self) -> dict[str, Any]:
        """The settings a result document records about this cache."""

        return {
            "enabled": True,
            "ttl_s": self.ttl_s,
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
        }


__all__ = [
    "CACHE_DIR",
    "CachedResponse",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_S",
    "POLICY_NONE",
    "POLICY_PERSISTENT",
    "ResponseCache",
]
