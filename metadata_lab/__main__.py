"""``python -m metadata_lab`` entry point (works without the lab venv shim)."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
