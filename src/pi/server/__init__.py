"""pi-py multi-user server package.

`create_app` / `ServerSettings` resolve lazily. Importing them eagerly here made
every leaf module drag in the FastAPI app: `pi.server.db` -> this __init__ ->
`pi.server.app` -> `pi.observability.metering` -> `pi.server.db` (partially
initialized) -> ImportError. Any script that reached for metering or db before
the app hit that cycle.
"""

from __future__ import annotations

from importlib import import_module

_LAZY = {"create_app": "pi.server.app", "ServerSettings": "pi.server.config"}

__all__ = ["create_app", "ServerSettings"]


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(_LAZY[name]), name)
