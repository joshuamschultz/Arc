"""Execute a ``.py`` file as a module under a caller-chosen name.

One mechanic, two callers: the capability loader (synthetic per-path names, so
two ``echo.py`` files at different scan roots stay distinct objects) and the
module-runtime loader (the canonical dotted name, so a module's own
``capabilities.py`` reaches the same runtime object it configured).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

__all__ = ["exec_source_module"]


def exec_source_module(
    path: Path,
    module_name: str,
    *,
    restricted_builtins: dict[str, object] | None = None,
) -> ModuleType:
    """Compile and execute *path*, registered in ``sys.modules`` as *module_name*.

    Reads source + ``compile()`` + ``exec`` directly instead of going through
    ``spec.loader.exec_module``. The latter consults importlib's ``__pycache__``
    bytecode cache, which is keyed by source mtime — and HFS+ / older APFS /
    some CI runners report 1-second mtime resolution. Two writes inside the same
    second produce identical mtimes, and the second load silently serves the
    first version's bytecode. The explicit ``compile()`` path bypasses
    ``__pycache__`` entirely so a reload is always honest about file content.

    When *restricted_builtins* is supplied (workspace-authored source), the
    namespace is seeded with it BEFORE ``exec`` so the source runs under the
    restricted surface + wrapped ``__import__`` instead of the full builtin
    set — pre-seeding ``__builtins__`` makes ``exec`` use it rather than
    injecting real builtins. First-party callers pass None and keep the trusted
    import path.

    A failed execution pops the registration before re-raising: a half-executed
    module must never stay visible to a later import.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:
        raise ImportError(f"could not build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if restricted_builtins is not None:
        module.__dict__["__builtins__"] = restricted_builtins
    try:
        code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 — executing first-party/gated source is the point
    except Exception:  # reason: never leave a half-executed module registered
        sys.modules.pop(module_name, None)
        raise
    return module
