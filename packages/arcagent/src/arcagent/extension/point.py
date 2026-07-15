"""SPEC-047 — the ``ExtensionPoint`` descriptor.

A frozen descriptor that parametrizes everything that *differs* between the select-one
extension seams (Brain / SPEC-041, SkillAdapter / SPEC-044); everything that is the
*same* — the choice dispatch, the fail-closed BYO allowlist gate, the dotted-path
importer — lives once in :func:`arcagent.extension.select.select_extension`.

A seam picks ONE of two ways to resolve a non-null, non-BYO choice:

* **builtin_modules + builtin_builder** — a fixed choice→module map the framework knows
  the construction shape of (SkillAdapter's ``arcskill``).
* **provider_entrypoint** — a *generic* convention: the choice string IS a backend module
  name the framework imports and whose well-known factory (``provider_entrypoint(context)``)
  builds the instance. The framework names no backend — this is how Brain resolves an
  external memory backend package without importing or naming it.

BYO (a dotted ``module:Class`` path) is always available and always allowlist-gated above
personal, regardless of which resolution the seam uses.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ExtensionPoint:
    """Declarative descriptor for one extension seam.

    Attributes:
        name: Short family name ("brain" | "skills" | ...), used in log/audit
            messages and inspection output.
        null_factory: Zero-arg callable returning the Null default (memory/improver
            off) when the setting is ``none``/``""``/``null`` or a builtin degrades.
        byo_constructor: ``(resolved_cls, context) -> instance``. Constructs a BYO
            implementation from an already-imported+allowlist-checked class. Seams
            differ here (``cls(ws, did)`` vs ``cls(ws)``), so it is a field.
        builtin_modules: Maps a builtin choice string to the *actual import string*
            (e.g. ``{"arcskill": "arcskill.improver"}``). Empty for a seam that resolves
            backends generically via ``provider_entrypoint``. ``auto`` degrades silently;
            any other builtin choice warns when its module is unavailable.
        builtin_builder: ``(imported_module, context) -> instance | None``. Receives the
            lazily-imported builtin module and the seam's context dict; returns ``None``
            to degrade to the Null default. ``None`` when the seam declares no builtins.
        provider_entrypoint: The well-known factory attribute a generic backend module
            exposes (e.g. ``"build_brain"``). When set, a bare (non-BYO) choice string is
            treated as a backend module name: it is lazily imported and its
            ``provider_entrypoint(context)`` builds the instance; a missing package or
            entrypoint degrades to the Null default with a warning. ``None`` disables the
            generic provider path (the seam then routes bare choices to BYO).
        kind: ``"select_one"`` (this mechanism) or ``"scan_many"`` (a view over the
            SPEC-021 capability registry, declared in :mod:`arcagent.extension.families`).
    """

    name: str
    null_factory: Callable[[], Any]
    byo_constructor: Callable[[type, dict[str, Any]], Any]
    builtin_modules: Mapping[str, str] = field(default_factory=dict)
    builtin_builder: Callable[[Any, dict[str, Any]], Any | None] | None = None
    provider_entrypoint: str | None = None
    kind: Literal["select_one", "scan_many"] = "select_one"


__all__ = ["ExtensionPoint"]
