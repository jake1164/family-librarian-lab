"""`./lab metadata ...` pass-through.

This module exists so `family_librarian_lab/commands.py` does not grow: it only
registers one command that hands the remaining arguments to
``metadata_lab.cli.main``. It imports nothing from the metadata package at import
time, so a broken third-party dependency cannot break the whole lab CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent import registry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _configure_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "metadata_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to metadata_lab, e.g. 'providers', 'run fourth-wing-empyrean'",
    )


@registry.command(
    "metadata",
    help="Read-only metadata provider comparison; see docs/03-metadata-comparison-lab.md",
    configure=_configure_metadata,
)
def handle_metadata(args: argparse.Namespace, config: object) -> int:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from metadata_lab.cli import main as metadata_main
    except ImportError as exc:  # a missing dependency must not look like a lab bug
        print(f"metadata: cannot import metadata_lab ({exc})", file=sys.stderr)
        return 2
    return metadata_main(list(getattr(args, "metadata_args", []) or []))
