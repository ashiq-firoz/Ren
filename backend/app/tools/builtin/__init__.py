"""Built-in tool groups. Every module in this package is imported at startup; modules create
`ToolGroup(...)` instances (which self-register) and decorate tool functions."""
from __future__ import annotations

import importlib
import logging
import pkgutil

log = logging.getLogger("ren.tools")

_loaded = False


def load_builtin_tools() -> list[str]:
    global _loaded
    loaded: list[str] = []
    if _loaded:
        return loaded
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
            loaded.append(mod.name)
        except Exception:  # noqa: BLE001
            log.exception("failed to load tool module %s", mod.name)
    _loaded = True
    return loaded
