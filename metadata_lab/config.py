"""Credential and settings lookup.

Credentials are read server-side only: environment first, then a plain
``KEY=VALUE`` parse of ``lab.env`` at the repo root. Values are never logged,
formatted or stored on an object that gets serialised. A blank value means
"not configured", which is a valid state (the adapter reports
``not_configured`` instead of failing).
"""

from __future__ import annotations

import os
from pathlib import Path

LAB_ENV_FILENAME = "lab.env"

# Contact detail for outbound User-Agent strings. Explicit opt-in only; nothing
# personal is ever read from the lab's product configuration.
CONTACT_ENV = "FAMILY_LIBRARIAN_LAB_METADATA_CONTACT"


def repo_root() -> Path:
    """Return the repository root (the parent of this package)."""

    return Path(__file__).resolve().parent.parent


def lab_env_path() -> Path:
    return repo_root() / LAB_ENV_FILENAME


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_lab_env(path: Path | None = None) -> dict[str, str]:
    """Parse ``lab.env`` into a mapping of non-blank values.

    Unknown syntax is ignored rather than raising: a broken unrelated line must
    not stop the tool from starting.
    """

    target = path or lab_env_path()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        parsed = _unquote(value)
        if parsed:
            values[key] = parsed
    return values


def get_credential(env_name: str, *aliases: str) -> str | None:
    """Return the first non-blank value for ``env_name`` or its aliases.

    Environment variables win over ``lab.env``. The returned value is never
    logged; callers pass it straight to ``secret_params`` / ``secret_headers``.
    ``None`` means "not configured".
    """

    names = (env_name, *aliases)
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value

    from_file = read_lab_env()
    for name in names:
        value = from_file.get(name, "").strip()
        if value:
            return value
    return None


def resolve_credential(adapter: object) -> str | None:
    """Credential for an adapter instance/class, honouring declared aliases."""

    env_name = getattr(adapter, "credential_env", None)
    if not env_name:
        return None
    aliases = tuple(getattr(adapter, "credential_env_aliases", ()) or ())
    return get_credential(str(env_name), *aliases)


def is_configured(adapter: object) -> bool:
    """True for keyless adapters, or when a credential can be resolved."""

    if not getattr(adapter, "credential_env", None):
        return True
    return resolve_credential(adapter) is not None


def contact_detail() -> str | None:
    """Optional contact string appended to the User-Agent, if explicitly set."""

    value = os.environ.get(CONTACT_ENV, "").strip()
    if value:
        return value
    return read_lab_env().get(CONTACT_ENV, "").strip() or None


__all__ = [
    "CONTACT_ENV",
    "contact_detail",
    "get_credential",
    "is_configured",
    "lab_env_path",
    "read_lab_env",
    "repo_root",
    "resolve_credential",
]
