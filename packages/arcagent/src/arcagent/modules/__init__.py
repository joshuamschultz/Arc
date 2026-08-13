"""Official ArcAgent modules — the package name; the deployment owns the contents.

This file is the ONLY thing under ``arcagent/modules/`` that ships in the wheel
(SPEC-066 REQ-336). A module reaches a box as a signed bundle materialized to
``${ARC_CONFIG_DIR:-~/.arc}/modules/``, so a deployment that was never approved
for the browser module does not carry its source — absence is a directory
listing, not a config flag somebody could flip.

**Why the name still has to resolve.** Module code addresses itself and its
siblings by dotted name — ``from arcagent.modules.browser import _runtime``, and
eighty-odd more like it, with no relative imports anywhere in the catalog. A
materialized ``capabilities.py`` runs those imports for real. If
``arcagent.modules`` stopped being an importable package the moment its source
left the wheel, every installed module would fail on its first import while the
bundle it came from verified perfectly.

So the package survives and its ``__path__`` spans both places a module can
legitimately live:

* this directory, which holds the catalog in a source checkout and is empty in a
  wheel — first, so a developer's checkout behaves exactly as it always has;
* the deployment module root, which is where an installed bundle lands and, in a
  wheel install, the only place modules exist at all.

``__path__`` is recomputed on every traversal rather than frozen at import.
``ARC_CONFIG_DIR`` is routinely set *after* this module is first imported — by a
test, or by a service that exports it in its unit file — and a list captured at
import time would pin whatever the environment happened to say back then. This
is the same reason :func:`~arcagent.core.module_discovery.module_root` resolves
per call instead of caching a module-level constant.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from arcagent.core.module_discovery import module_root

_PACKAGE_DIR = Path(__file__).parent


class _ModuleSearchPath(list[str]):
    """A ``__path__`` that reports the CURRENT set of module locations.

    Subclasses :class:`list` because the import machinery, and plenty of tooling
    around it, expects ``__path__`` to be one. Everything is served from
    :meth:`__iter__`, which the finder consults on every submodule import, so
    the answer tracks ``ARC_CONFIG_DIR`` instead of a snapshot of it.
    """

    def __iter__(self) -> Iterator[str]:
        locations = [_PACKAGE_DIR]
        deployment = module_root()
        if deployment != _PACKAGE_DIR:
            locations.append(deployment)
        return iter([str(path) for path in locations if path.is_dir()])


__path__ = _ModuleSearchPath()
