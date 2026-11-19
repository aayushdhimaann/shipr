"""Provider registry. Third-party packages add providers via the `shipr.providers` entry point."""

from __future__ import annotations

from importlib.metadata import entry_points

from ..errors import ConnectionLost, ExecNotSupported, ProviderError
from .base import Provider, RemoteStat

__all__ = [
    "ConnectionLost", "ExecNotSupported", "Provider", "ProviderError", "RemoteStat", "get",
    "provider_names",
]


def _registry() -> dict[str, object]:
    """Provider name -> entry point (or "module:Class" for built-ins)."""
    eps = {ep.name: ep for ep in entry_points(group="shipr.providers")}
    # built-ins work even when running from a source tree without installed metadata
    eps.setdefault("sftp", "shipr.providers.sftp:SFTPProvider")
    eps.setdefault("local", "shipr.providers.local:LocalProvider")
    return eps


def provider_names() -> set[str]:
    return set(_registry())


def get(name: str) -> type[Provider]:
    ep = _registry().get(name)
    if ep is None:
        raise ProviderError(f"unknown provider {name!r}")
    if isinstance(ep, str):
        import importlib

        module, attr = ep.split(":")
        return getattr(importlib.import_module(module), attr)
    return ep.load()
