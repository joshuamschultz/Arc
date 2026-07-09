"""SPEC-047 — the first-class extension-point framework.

``arcagent/extension/`` (sibling of ``core/``, like ``brain/`` and ``skilladapt/``) holds
the generalized select-one mechanism plus the family registry + inspection for the CLI:

* :class:`ExtensionPoint` + :func:`select_extension` — the one copy of the choice
  dispatch, fail-closed BYO allowlist gate, and dotted-path importer (SPEC-041/044 dedup).
* ``families`` — the four extension-point families (brain, skills, tools, hook-builds).
* ``inspect`` — a pure read of what is selected / available / signed for ``arc ext inspect``.

The mechanism speaks only structural Protocols + primitives; it never names a concrete
implementation type or statically imports a builtin/BYO package.
"""

from __future__ import annotations

from arcagent.extension.point import ExtensionPoint
from arcagent.extension.select import select_extension

__all__ = ["ExtensionPoint", "select_extension"]
