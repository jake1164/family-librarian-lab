"""Per-provider pacing and the persisted cool-down record.

**The intervals below are research-based engineering choices, not provider-granted
quotas.** Each adapter's value comes from a published limit collected in
``.ai_docs/metadata/metadata-research.md`` plus a safety margin, or from lab
judgement where no numeric limit is published. None of them entitles this tool to
issue that many requests.

The cool-down record is one JSON file at ``<results root>/cooldowns.json``
(``results/`` is gitignored, so it is host-local state, never a repository
artefact). A missing, unreadable or corrupt file reads as "nothing is cooling
down": losing it must never stop a run.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "metadata-lab.cooldowns/1"
FILENAME = "cooldowns.json"
#: ``reason`` recorded with an entry: the provider answered HTTP 429.
REASON_RATE_LIMITED = "rate_limited"
#: Cool-down when a provider sends no ``Retry-After``; adapters may override it.
DEFAULT_COOLDOWN_S = 15 * 60.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Cooldown:
    """One provider is not called again until ``until``."""

    provider: str
    until: datetime
    reason: str
    observed_at: datetime

    def is_active(self, now: datetime) -> bool:
        return self.until > now

    def minutes_remaining(self, now: datetime) -> int:
        seconds = (self.until - now).total_seconds()
        return max(0, math.ceil(seconds / 60.0))

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "until": _format_timestamp(self.until),
            "reason": self.reason,
            "observed_at": _format_timestamp(self.observed_at),
        }

    @classmethod
    def from_dict(cls, provider: str, payload: Any) -> "Cooldown | None":
        if not isinstance(payload, Mapping):
            return None
        until = _parse_timestamp(payload.get("until"))
        observed = _parse_timestamp(payload.get("observed_at"))
        if until is None or observed is None:
            return None
        return cls(
            provider=str(payload.get("provider") or provider),
            until=until,
            reason=str(payload.get("reason") or REASON_RATE_LIMITED),
            observed_at=observed,
        )


class CooldownStore:
    """Reads and writes the one cool-down file. Never raises for file trouble."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.path = Path(path)
        self._clock = clock

    @classmethod
    def under(cls, results_root: Path | str, **kwargs: Any) -> "CooldownStore":
        """The store that belongs to one results directory."""

        return cls(Path(results_root) / FILENAME, **kwargs)

    def now(self) -> datetime:
        return self._clock()

    # -- reading ---------------------------------------------------------------

    def _read(self) -> Mapping[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Missing, unreadable or corrupt: treat as "nothing is cooling".
            return {}
        return payload if isinstance(payload, Mapping) else {}

    def load(self) -> dict[str, Cooldown]:
        """Every entry in the file, expired ones included."""

        providers = self._read().get("providers")
        if not isinstance(providers, Mapping):
            return {}
        entries: dict[str, Cooldown] = {}
        for provider_id, payload in providers.items():
            entry = Cooldown.from_dict(str(provider_id), payload)
            if entry is not None:
                entries[entry.provider] = entry
        return entries

    def cooling(self) -> dict[str, Cooldown]:
        """Only the entries that are still in force."""

        now = self.now()
        return {
            provider_id: entry
            for provider_id, entry in self.load().items()
            if entry.is_active(now)
        }

    def active(self, provider_id: str) -> Cooldown | None:
        entry = self.load().get(provider_id)
        if entry is None or not entry.is_active(self.now()):
            return None
        return entry

    def minutes_remaining(self, provider_id: str) -> int | None:
        entry = self.active(provider_id)
        return None if entry is None else entry.minutes_remaining(self.now())

    def note_for(self, provider_id: str) -> str | None:
        """A short human note naming the provider and the time left."""

        entry = self.active(provider_id)
        if entry is None:
            return None
        minutes = entry.minutes_remaining(self.now())
        observed = _format_timestamp(entry.observed_at)
        return (
            f"{provider_id} is cooling down ({entry.reason} seen {observed}), "
            f"about {minutes} min remaining"
        )

    # -- writing ---------------------------------------------------------------

    def _save(self, entries: Mapping[str, Cooldown]) -> None:
        payload = {
            "schema": SCHEMA,
            "providers": {
                provider_id: entry.to_dict() for provider_id, entry in sorted(entries.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            # A read-only or missing results directory must not crash a run.
            pass

    def record(
        self,
        provider_id: str,
        *,
        retry_after_s: float | None = None,
        default_s: float = DEFAULT_COOLDOWN_S,
        reason: str = REASON_RATE_LIMITED,
    ) -> Cooldown:
        """Write (or extend) one entry, using ``Retry-After`` when it was sent."""

        now = self.now()
        seconds = retry_after_s if retry_after_s is not None and retry_after_s > 0 else default_s
        until = now + timedelta(seconds=max(0.0, float(seconds)))
        entries = self.load()
        existing = entries.get(provider_id)
        if existing is not None and existing.until > until:
            # A longer block already known about wins; never shorten it.
            until = existing.until
        entry = Cooldown(provider=provider_id, until=until, reason=reason, observed_at=now)
        entries[provider_id] = entry
        self._save(entries)
        return entry

    def clear(self, provider_id: str) -> None:
        entries = self.load()
        if entries.pop(provider_id, None) is not None:
            self._save(entries)

    def clear_expired(self) -> list[str]:
        """Drop entries that are no longer in force; return the removed ids."""

        now = self.now()
        entries = self.load()
        removed = [
            provider_id for provider_id, entry in entries.items() if not entry.is_active(now)
        ]
        if removed:
            for provider_id in removed:
                del entries[provider_id]
            self._save(entries)
        return sorted(removed)


__all__ = [
    "Cooldown",
    "CooldownStore",
    "DEFAULT_COOLDOWN_S",
    "FILENAME",
    "REASON_RATE_LIMITED",
    "SCHEMA",
    "utc_now",
]
