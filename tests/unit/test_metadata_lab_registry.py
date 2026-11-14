"""Adapter discovery from a provider package."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from metadata_lab import registry


def _write_package(root: Path, name: str, modules: dict[str, str]) -> None:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for module_name, source in modules.items():
        (package / f"{module_name}.py").write_text(textwrap.dedent(source), encoding="utf-8")


ADAPTER_SOURCE = """
    from typing import ClassVar

    from metadata_lab.providers.base import ProviderAdapter


    class Adapter(ProviderAdapter):
        id: ClassVar[str] = "{id}"
        display_name: ClassVar[str] = "Test {id}"
"""


def test_temporary_package_adapter_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_package(tmp_path, "mdlab_tmp_providers", {"alpha": ADAPTER_SOURCE.format(id="alpha")})
    monkeypatch.syspath_prepend(str(tmp_path))

    found = registry.discover("mdlab_tmp_providers")

    assert list(found) == ["alpha"]
    assert found["alpha"].display_name == "Test alpha"


def test_underscore_modules_are_not_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_package(
        tmp_path,
        "mdlab_tmp_skip",
        {
            "alpha": ADAPTER_SOURCE.format(id="alpha"),
            "_template": ADAPTER_SOURCE.format(id="ignored"),
            "base": "IGNORED = True",
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    found = registry.discover("mdlab_tmp_skip")

    assert list(found) == ["alpha"]


def test_duplicate_ids_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_package(
        tmp_path,
        "mdlab_tmp_dupes",
        {
            "alpha": ADAPTER_SOURCE.format(id="same"),
            "beta": ADAPTER_SOURCE.format(id="same"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="duplicate provider id 'same'"):
        registry.discover("mdlab_tmp_dupes")


def test_missing_id_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_package(
        tmp_path,
        "mdlab_tmp_noid",
        {
            "alpha": """
                from metadata_lab.providers.base import ProviderAdapter


                class Adapter(ProviderAdapter):
                    display_name = "No id"
            """
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ValueError, match="has no id"):
        registry.discover("mdlab_tmp_noid")


def test_shipped_template_is_not_registered():
    # The real package ships _template.py; discovery must ignore it.
    sys.modules.pop("metadata_lab.providers._template", None)

    assert "example-provider" not in registry.discover()
