"""Settings -> Maintenance: the routes behind the tab, registered as one group.

Each section lives in its own module so it can be read, tested and replaced on its
own; this file only lists them, so ``server.py`` registers the whole tab with one line.
"""

from __future__ import annotations

from starlette.routing import Route

from arcui.routes import maintenance_modules, maintenance_runtime

routes: list[Route] = [
    *maintenance_runtime.routes,
    *maintenance_modules.routes,
]

__all__ = ["routes"]
