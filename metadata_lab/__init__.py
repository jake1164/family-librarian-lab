"""Standalone, read-only metadata comparison tooling.

This package deliberately imports nothing from ``family_librarian_lab`` or the
``agent`` package shipped in ``se-lab``: it must run on a machine with only
Python, ``requests`` and ``PyYAML``.
"""

from __future__ import annotations

VERSION = "0.1.0"

__all__ = ["VERSION"]
