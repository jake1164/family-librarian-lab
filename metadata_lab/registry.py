"""Adapter discovery: the reason "adding a provider" is a one-file change."""

from __future__ import annotations

import importlib
import pkgutil

from .model import Operation
from .providers.base import ProviderAdapter

_SKIP_MODULES = {"base"}


DEFAULT_PACKAGE = "metadata_lab.providers"


def discover(package_name: str = DEFAULT_PACKAGE) -> dict[str, type[ProviderAdapter]]:
    """Import every adapter module and return ``{provider_id: adapter_class}``.

    Modules whose name starts with ``_`` are skipped (``_template`` is the
    copy-me skeleton). A duplicate ``id`` is a programming error and raises.
    ``package_name`` exists so tests can point discovery at a temporary package.
    """

    package = importlib.import_module(package_name)
    found: dict[str, type[ProviderAdapter]] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if name.startswith("_") or name in _SKIP_MODULES:
            continue
        module = importlib.import_module(f"{package_name}.{name}")
        for attribute in vars(module).values():
            if not isinstance(attribute, type) or not issubclass(attribute, ProviderAdapter):
                continue
            if attribute is ProviderAdapter or attribute.__module__ != module.__name__:
                continue
            provider_id = getattr(attribute, "id", "")
            if not provider_id:
                raise ValueError(f"adapter {attribute.__name__} in {module.__name__} has no id")
            if provider_id in found:
                raise ValueError(
                    f"duplicate provider id {provider_id!r} in {module.__name__} "
                    f"(already defined by {found[provider_id].__module__})"
                )
            found[provider_id] = attribute
    return dict(sorted(found.items()))


def sorted_operations() -> list[Operation]:
    """Stable operation order for reports and matrices."""

    return list(Operation)


__all__ = ["discover", "sorted_operations"]
