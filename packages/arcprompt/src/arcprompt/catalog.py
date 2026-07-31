"""PromptCatalog — discover every packaged prompt across installed Arc packages (COMP-005).

Stock prompts ship as ``src/<pkg>/context/<name>.md`` inside each owning
package's wheel. The catalog locates each package's ``context/`` directory via
``importlib`` (the ``Path(module.__file__).parent`` convention from
``blueprints/loader.py``) and enumerates the markdown within, so a caller can
list a prompt that has *no* overlay (REQ-134). It reads stock only and never
touches overlay state.

A package named in the scan list but not installed is skipped silently — a
deployment that does not ship arcskill simply has no arcskill prompts to manage,
which is not an error. A *missing individual stock file* for a prompt that is
asked for by name is a packaging error, but that is the resolver's concern
(REQ-126); the catalog only reports what is physically present.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from arcprompt.document import PromptDocument, parse_prompt
from arcprompt.errors import PromptMissing

# The prompt-body resolver contract: ``(package, name) -> effective body``. The
# shared type a consumer accepts to be handed overlay-aware resolution without
# depending on the concrete PromptResolver (e.g. arcrun's get_strategy_prompts).
PromptResolve = Callable[[str, str], str]

CONTEXT_DIRNAME = "context"

# A package/name is always a single path component (a slug like ``arcrun`` /
# ``strategy_react``). Reject anything that could traverse out of the packaged
# ``context/`` dir or an agent's overlay root when joined into a filesystem path
# (SEC-04) — separators, NUL, or a bare ``.``/``..`` component. This is enforced
# at the arcprompt chokepoint so no caller (arcui route, arccli argv, agent) can
# turn a prompt lookup into an arbitrary ``.md`` read, regardless of its own diligence.
_PATH_UNSAFE_CHARS = ("/", "\\", "\x00")


def _ensure_safe(package: str, name: str) -> None:
    """Raise :class:`PromptMissing` if ``package``/``name`` is not a safe path component."""
    for value in (package, name):
        if not value or value in (".", "..") or any(c in value for c in _PATH_UNSAFE_CHARS):
            raise PromptMissing(package, name)


# Arc packages that ship harness prompts. A package absent from a deployment is
# skipped; adding a new prompt-shipping package means adding it here.
DEFAULT_PROMPT_PACKAGES: tuple[str, ...] = ("arcrun", "arcagent", "arcmemory", "arcskill")


class PromptRef(BaseModel):
    """A discovered stock prompt: enough to list and locate it without loading overlays."""

    model_config = ConfigDict(frozen=True)

    package: str
    name: str
    description: str
    stock_path: Path


def _package_context_dir(package: str) -> Path | None:
    """Return the installed package's ``context/`` dir, or None if absent/uninstalled."""
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    context = Path(spec.origin).parent / CONTEXT_DIRNAME
    return context if context.is_dir() else None


class PromptCatalog:
    """Enumerate stock prompts across a fixed set of installed packages."""

    def __init__(self, packages: Sequence[str] = DEFAULT_PROMPT_PACKAGES) -> None:
        self._packages = tuple(packages)

    def catalog(self) -> list[PromptRef]:
        """Return every discoverable stock prompt, sorted by (package, name)."""
        refs: list[PromptRef] = []
        for package in self._packages:
            context = _package_context_dir(package)
            if context is None:
                continue
            for md in sorted(context.glob("*.md")):
                doc = parse_prompt(md.read_bytes(), source="stock")
                refs.append(
                    PromptRef(
                        package=package,
                        name=doc.name,
                        description=doc.description,
                        stock_path=md,
                    )
                )
        return sorted(refs, key=lambda r: (r.package, r.name))

    def stock_path(self, package: str, name: str) -> Path | None:
        """Return the stock file path for one prompt, or None if not packaged."""
        _ensure_safe(package, name)
        context = _package_context_dir(package)
        if context is None:
            return None
        candidate = context / f"{name}.md"
        return candidate if candidate.is_file() else None


def load_stock_document(package: str, name: str) -> PromptDocument:
    """Load one package's *stock* prompt directly, ignoring overlays.

    This is the consumer entry point for a package loading its own shipped
    prompt (``arcrun``, ``arcagent``, ``arcmemory`` import arcprompt and call
    this). Overlay-aware resolution is :class:`~arcprompt.resolver.PromptResolver`,
    threaded through the agent at run start; this convenience is stock-only.

    Raises :class:`PromptMissing` if the package ships no such prompt — a
    packaging error naming package and prompt (REQ-126) — or if the identifiers
    are not safe path components (SEC-04).
    """
    _ensure_safe(package, name)
    context = _package_context_dir(package)
    if context is None:
        raise PromptMissing(package, name)
    path = context / f"{name}.md"
    if not path.is_file():
        raise PromptMissing(package, name)
    return parse_prompt(path.read_bytes(), source="stock")


def load_stock(package: str, name: str) -> str:
    """Return the *body* of a package's stock prompt (see :func:`load_stock_document`)."""
    return load_stock_document(package, name).body


__all__ = [
    "CONTEXT_DIRNAME",
    "DEFAULT_PROMPT_PACKAGES",
    "PromptCatalog",
    "PromptRef",
    "PromptResolve",
    "load_stock",
    "load_stock_document",
]
