#!/usr/bin/env python3
"""Family Librarian lab entry point, invoked by the ``lab`` shim."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from agent.runtime import configure

configure(
    repo_root=REPO_ROOT,
    product_name="family-librarian",
    env_prefix="FAMILY_LIBRARIAN",
)

# Registration must happen before the shared CLI parser is constructed.
import family_librarian_lab.commands  # noqa: F401, E402

from agent.cli import main  # noqa: E402


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user.", flush=True)
        raise SystemExit(130)
