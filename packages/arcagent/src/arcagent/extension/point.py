"""SPEC-047 — the ``ExtensionPoint`` descriptor.

A frozen descriptor that parametrizes everything that *differs* between two
select-one extension seams (Brain / SPEC-041, SkillAdapter / SPEC-044); everything
that is the *same* — the choice dispatch, the fail-closed BYO allowlist gate, the
dotted-path importer — lives once in :func:`arcagent.extension.select.select_extension`.

The two seams differ only in: the Null default, which builtin module a choice imports,
how the imported module is turned into an instance, and how a BYO class is constructed.
Those four axes are exactly the fields below, so a third extension point *declares* an
``ExtensionPoint`` instead of copying the mechanism a third time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ExtensionPoint:
    """Declarative descriptor for one extension seam.

    Attributes:
        name: Short family name ("brain" | "skills" | ...), used in log/audit
            messages and inspection output.
        null_factory: Zero-arg callable returning the Null default (memory/improver
            off) when the setting is ``none``/``""``/``null`` or a builtin degrades.
        builtin_modules: Maps a builtin choice string to the *actual import string*
            (e.g. ``{"arcmemory": "arcmemory", "auto": "arcmemory"}`` or
            ``{"arcskill": "arcskill.improver"}``). ``auto`` degrades silently; any
            other builtin choice warns when its module is unavailable.
        builtin_builder: ``(imported_module, context) -> instance | None``. Receives
            the lazily-imported builtin module and the seam's context dict; returns
            ``None`` to degrade to the Null default. The import is lifted out into
            :func:`select_extension` so this callable never imports.
        byo_constructor: ``(resolved_cls, context) -> instance``. Constructs a BYO
            implementation from an already-imported+allowlist-checked class. The two
            seams differ here (``cls(ws, did)`` vs ``cls(ws)``), so it is a field.
        kind: ``"select_one"`` (this mechanism) or ``"scan_many"`` (a view over the
            SPEC-021 capability registry, declared in :mod:`arcagent.extension.families`).
    """

    name: str
    null_factory: Callable[[], Any]
    builtin_modules: Mapping[str, str]
    builtin_builder: Callable[[Any, dict[str, Any]], Any | None]
    byo_constructor: Callable[[type, dict[str, Any]], Any]
    kind: Literal["select_one", "scan_many"] = "select_one"


__all__ = ["ExtensionPoint"]
