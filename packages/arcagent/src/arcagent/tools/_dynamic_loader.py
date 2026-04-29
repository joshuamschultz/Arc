"""Dynamic-tool AST validator — SPEC-017 Phase 7 R-053.

Layered defense against agent-generated RCE:

  1. Encoding check  — reject non-UTF-8 source declarations (codec
     attacks run BEFORE AST parsing, so a regex on the raw text is
     the only safe gate).
  2. AST walk       — reject privileged imports, frame traversal,
     dynamic exec, __builtins__ mutation, ``__init_subclass__``
     mutation, starred ``__builtins__`` unpacking.

RestrictedPython has repeatedly shipped CVEs where AST scans missed
bypasses (CVE-2023-37271, CVE-2024-47532, CVE-2025-22153). Our
approach is narrow by design: reject anything we don't understand;
safer to refuse a legal program than to accept a malicious one.

This module provides the *first* layer of the dynamic-tool pipeline.
Runtime restriction (scrubbed ``__builtins__``, egress proxy, policy
pipeline) is layered on top — they together form the R-053 / R-054
defense.
"""

from __future__ import annotations

import ast
import builtins as _builtins
import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

from arcagent.core.errors import ArcAgentError

# --- Rejection categories -------------------------------------------------

_BLOCKED_IMPORTS: frozenset[str] = frozenset(
    {
        "ctypes",
        "subprocess",
        "socket",
        "os",
        "sys",
        "pickle",
        "marshal",
        "shelve",
    }
)

_BLOCKED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # Frame / generator / traceback internals (CVE-2023-37271 class)
        "gi_frame",
        "gi_code",
        "gi_yieldfrom",
        "tb_frame",
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
        # Class-graph traversal (CVE-2024-47532 class)
        "__class__",
        "__bases__",
        "__subclasses__",
        "__init_subclass__",
        "__class_getitem__",
        "__reduce__",
        "__reduce_ex__",
        "__mro__",
        "__dict__",
        # Descriptor protocol side-channels — used in subscript bypasses
        # to reach bound descriptor objects.
        "__get__",
        "__set__",
        "__pos__",
        "__neg__",
        "modules",  # guards against m.modules['os'] via any alias
    }
)

# AttributeError instances expose ``obj`` and ``name`` (Python 3.10+) —
# these leak the failed-access target. Reject within ``except`` bindings
# that catch ``AttributeError``.
_ATTRIBUTE_ERROR_LEAKED_ATTRS: frozenset[str] = frozenset({"obj", "name"})

_BLOCKED_CALLS: frozenset[str] = frozenset(
    {
        "compile",
        "eval",
        "exec",
        "__import__",
    }
)

_BLOCKED_ASSIGN_TARGETS: frozenset[str] = frozenset(
    {
        "__builtins__",
        "__loader__",
        "__spec__",
    }
)

# Matches PEP 263 ``coding`` declarations in the first two lines of a
# source file. Anything other than utf-8 is rejected.
_CODING_DECL_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)", re.MULTILINE)


# --- Errors ---------------------------------------------------------------


class ASTValidationError(ArcAgentError):
    """Raised when dynamic source fails static validation."""

    _component = "dynamic_loader"

    def __init__(self, *, category: str, detail: str) -> None:
        super().__init__(
            code="DYNAMIC_TOOL_AST_REJECTED",
            message=f"{category} — {detail}",
            details={"category": category, "detail": detail},
        )
        self.category = category


# --- Validator ------------------------------------------------------------


class AstValidator(ast.NodeVisitor):
    """Walks a parsed AST and rejects prohibited patterns.

    Stateful — a single instance tracks the first violation seen and
    raises immediately. Construct fresh per source file.
    """

    def __init__(self) -> None:
        self._violation: tuple[str, str] | None = None
        # Names bound by ``except AttributeError as <name>`` — accessing
        # ``.obj`` / ``.name`` on these is rejected (R-040).
        self._attr_error_vars: set[str] = set()
        # Class-defs flagged as defining ``__getitem__`` so a class
        # using one as metaclass can be rejected at the consumer site.
        self._classes_with_getitem: set[str] = set()

    def validate(self, source: str) -> None:
        """Reject ``source`` if it contains a prohibited construct.

        Enforces the encoding check BEFORE parsing so codec-stage
        attacks never reach the AST.
        """
        self._check_encoding(source)
        tree = ast.parse(source, mode="exec")
        self.visit(tree)
        if self._violation is not None:
            category, detail = self._violation
            raise ASTValidationError(category=category, detail=detail)

    # --- Encoding ---------------------------------------------------------

    def _check_encoding(self, source: str) -> None:
        head = source.encode("utf-8", errors="replace").splitlines()[:2]
        for line in head:
            match = _CODING_DECL_RE.search(line)
            if match is None:
                continue
            encoding = match.group(1).decode("ascii").lower().replace("_", "-")
            if encoding not in {"utf-8", "utf8"}:
                raise ASTValidationError(
                    category="encoding:non_utf8",
                    detail=f"source coding declaration {encoding!r} not allowed",
                )

    # --- Record helper ----------------------------------------------------

    def _reject(self, category: str, detail: str) -> None:
        if self._violation is None:
            self._violation = (category, detail)

    # --- Visitors ---------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import_name(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is not None:
            self._check_import_name(node.module)
        self.generic_visit(node)

    def _check_import_name(self, module: str) -> None:
        top = module.split(".", 1)[0]
        if top in _BLOCKED_IMPORTS:
            self._reject("import:" + top, f"module {module!r} is blocked")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _BLOCKED_ATTRIBUTES:
            self._reject(f"attribute:{node.attr}", f"access to attribute {node.attr!r} is blocked")
        # Python 3.10+ ``AttributeError.obj`` / ``.name`` leak — reject
        # access on names bound by ``except AttributeError as <name>``.
        if (
            node.attr in _ATTRIBUTE_ERROR_LEAKED_ATTRS
            and isinstance(node.value, ast.Name)
            and node.value.id in self._attr_error_vars
        ):
            self._reject(
                f"exception_attr:{node.attr}",
                f"AttributeError.{node.attr} leak via {node.value.id!r}",
            )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Track ``except AttributeError as <name>:`` bindings.

        Scope: bindings are added before visiting handler body and
        removed after. Nested handlers stack correctly.
        """
        added_name = self._maybe_track_attribute_error(node)
        try:
            self.generic_visit(node)
        finally:
            if added_name is not None:
                self._attr_error_vars.discard(added_name)

    def _maybe_track_attribute_error(self, node: ast.ExceptHandler) -> str | None:
        if node.name is None or node.type is None:
            return None
        if not _is_attribute_error_type(node.type):
            return None
        self._attr_error_vars.add(node.name)
        return node.name

    def visit_FormattedValue(self, node: ast.FormattedValue) -> None:
        """Recurse into f-string interpolations.

        Default ``generic_visit`` already covers this, but the explicit
        override documents intent and protects against future AST
        shape changes.
        """
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Direct name call: eval(...), exec(...), etc.
        if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
            self._reject(
                f"call:{node.func.id}",
                f"call to {node.func.id!r} is blocked",
            )
        # Starred unpacking (f(*__builtins__))
        for arg in node.args:
            if isinstance(arg, ast.Starred) and _name_of(arg.value) in _BLOCKED_ASSIGN_TARGETS:
                self._reject(
                    f"starred:{_name_of(arg.value)}",
                    "starred unpacking of protected name",
                )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            name = _name_of(target)
            if name in _BLOCKED_ASSIGN_TARGETS:
                self._reject(f"assign:{name}", f"assignment to {name!r} is blocked")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if _class_defines_getitem(node):
            self._classes_with_getitem.add(node.name)
        for item in _iter_class_methods(node):
            if item.name == "__init_subclass__":
                self._reject(
                    "method:__init_subclass__",
                    "class must not define __init_subclass__",
                )
        if self._uses_blocked_metaclass(node):
            self._reject(
                "metaclass:__getitem__",
                f"class {node.name!r} uses metaclass that defines __getitem__",
            )
        self.generic_visit(node)

    def _uses_blocked_metaclass(self, node: ast.ClassDef) -> bool:
        for kw in node.keywords:
            if kw.arg != "metaclass":
                continue
            if isinstance(kw.value, ast.Name) and kw.value.id in self._classes_with_getitem:
                return True
        return False


def _name_of(node: ast.AST) -> str:
    """Return the leaf name of an AST node or empty string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _iter_class_methods(
    cls: ast.ClassDef,
) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    for item in cls.body:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            yield item


def _class_defines_getitem(cls: ast.ClassDef) -> bool:
    """True if ``cls`` declares an explicit ``__getitem__`` method."""
    return any(method.name == "__getitem__" for method in _iter_class_methods(cls))


def _is_attribute_error_type(node: ast.expr) -> bool:
    """Match ``AttributeError`` or ``(AttributeError, ...)`` exception clauses."""
    if isinstance(node, ast.Name) and node.id == "AttributeError":
        return True
    if isinstance(node, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id == "AttributeError" for elt in node.elts)
    return False


# --- AST validation cache -------------------------------------------------


class AstValidationCache:
    """Skip re-validation of unchanged files (R-001 perf gate).

    Keyed by absolute path → ``(md5, mtime)``. A second call with the
    same content + same mtime returns immediately. Either an mtime
    bump or a content hash change forces re-validation. Validation
    failures do NOT populate the cache, so the next attempt re-runs
    and re-raises (no false positives, no silent passes).
    """

    def __init__(self) -> None:
        self._entries: dict[Path, tuple[str, float]] = {}

    def validate(self, path: Path) -> None:
        """Validate ``path`` once per (md5, mtime) tuple.

        Reads the file, hashes it, compares against the last good run.
        On hit: returns. On miss: runs ``AstValidator().validate(...)``
        (re-uses module-level :class:`AstValidator` so monkey-patching
        in tests works) and stores the new fingerprint.
        """
        source = path.read_text(encoding="utf-8")
        # md5 is fine here — this is a cache key, not a security hash;
        # the security layer is the AstValidator + sandbox, not this.
        digest = hashlib.md5(source.encode("utf-8"), usedforsecurity=False).hexdigest()
        mtime = path.stat().st_mtime
        cached = self._entries.get(path)
        if cached == (digest, mtime):
            return
        # AstValidator looked up via module dict so test patches stick.
        from arcagent.tools import _dynamic_loader as _self

        _self.AstValidator().validate(source)
        self._entries[path] = (digest, mtime)

    def invalidate(self, path: Path) -> None:
        """Drop a single entry; called by the loader on file removal."""
        self._entries.pop(path, None)

    def __contains__(self, path: Path) -> bool:
        return path in self._entries


# --- Restricted builtins dict ---------------------------------------------

# Explicit allowlist. Anything missing raises NameError at runtime.
# Ordered by expected usage frequency for readability only.

_SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "print",
    "len",
    "range",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "sorted",
    "min",
    "max",
    "sum",
    "enumerate",
    "zip",
    "map",
    "filter",
    "any",
    "all",
    "isinstance",
    "repr",
    "reversed",
    "abs",
    "round",
    "divmod",
    "iter",
    "next",
    "hash",
    "hex",
    "oct",
    "bin",
    "chr",
    "ord",
    "type",
    "id",
    "None",
    "True",
    "False",
)

RESTRICTED_BUILTINS: dict[str, object] = {
    name: getattr(_builtins, name) for name in _SAFE_BUILTIN_NAMES if hasattr(_builtins, name)
}


# --- DynamicToolLoader ----------------------------------------------------

import logging  # noqa: E402
from collections.abc import Callable  # noqa: E402
from typing import Any, Literal  # noqa: E402

from arcagent.core.errors import ToolError  # noqa: E402
from arcagent.core.tool_registry import RegisteredTool, ToolTransport  # noqa: E402

_loader_logger = logging.getLogger("arcagent.tools.dynamic_loader")

CollisionPolicy = Literal["error", "replace", "warn", "ignore"]
AuditSink = Callable[[str, dict[str, Any]], None]


class DynamicToolLoader:
    """Load agent-authored Python source as a safe :class:`RegisteredTool`.

    Pipeline on each :meth:`load`:

      1. Encoding check  (reject non-UTF-8 coding declarations)
      2. AST validation  (:class:`AstValidator`)
      3. Compile with ``RESTRICTED_BUILTINS`` scrubbed dict
      4. Locate the ``@tool``-decorated async function
      5. Build :class:`RegisteredTool` with classification + tags
      6. Apply collision policy for previously-loaded names
      7. Emit structured audit event

    The loader is stateful — it remembers previously loaded tools so
    collisions can be detected. Reload semantics create a fresh
    module object each time; ``sys.modules`` is never mutated.

    Parameters
    ----------
    on_collision:
        What to do when ``name`` has already been loaded. ``"error"``
        raises, ``"replace"`` silently overwrites, ``"warn"`` logs
        and overwrites (default), ``"ignore"`` keeps the prior tool.
    audit_sink:
        Callback fired for every load attempt — both success
        (``dynamic_tool.loaded``) and failure (``dynamic_tool.rejected``).
    """

    def __init__(
        self,
        *,
        on_collision: CollisionPolicy = "warn",
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._on_collision = on_collision
        self._audit_sink = audit_sink
        self._loaded: dict[str, RegisteredTool] = {}

    def load(self, source: str, *, name: str) -> RegisteredTool:
        """Validate, compile, and register one tool from ``source``.

        Raises :class:`ASTValidationError` on static check failures,
        :class:`ToolError` on semantic problems (no decorated
        callable, namespace collision under ``"error"`` policy).
        """
        try:
            AstValidator().validate(source)
        except ASTValidationError as err:
            self._emit("dynamic_tool.rejected", {"name": name, "reason": str(err)})
            raise

        content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        module = self._compile_in_sandbox(source, name=name, content_hash=content_hash)
        tool_fn, meta = _find_decorated_tool(module)
        if tool_fn is None or meta is None:
            self._emit(
                "dynamic_tool.rejected",
                {"name": name, "reason": "no @tool-decorated function found"},
            )
            raise ToolError(
                code="DYNAMIC_TOOL_NO_DECORATOR",
                message=(f"Dynamic source for {name!r} contains no function decorated with @tool"),
                details={"name": name},
            )

        prior = self._loaded.get(name)
        if prior is not None:
            handled = self._handle_collision(name)
            if handled is False:
                # "ignore" — keep the prior tool as-is
                return prior

        async def _execute(**kwargs: Any) -> Any:
            return await tool_fn(**kwargs)

        registered = RegisteredTool(
            name=name,
            description=meta.description,
            input_schema=meta.input_schema,
            transport=ToolTransport.NATIVE,
            execute=_execute,
            source=f"dynamic:{name}:{content_hash}",
            classification=meta.classification,
            capability_tags=list(meta.capability_tags),
        )
        self._loaded[name] = registered
        self._emit(
            "dynamic_tool.loaded",
            {
                "name": name,
                "classification": meta.classification,
                "content_hash": content_hash,
            },
        )
        return registered

    def get(self, name: str) -> RegisteredTool | None:
        return self._loaded.get(name)

    def names(self) -> list[str]:
        return sorted(self._loaded)

    # --- Internals --------------------------------------------------------

    def _compile_in_sandbox(self, source: str, *, name: str, content_hash: str) -> dict[str, Any]:
        """Compile ``source`` into a fresh namespace with restricted builtins.

        The namespace doubles as the module's globals and is returned
        to the caller. No entry is added to ``sys.modules`` — SPEC-017
        R-052 forbids that to prevent leaks across reloads.

        ``__import__`` is wrapped by :func:`_make_restricted_import` so
        even if the AST validator misses a bypass, runtime import
        attempts against anything outside the safe surface raise
        :class:`ASTValidationError`. Defense in depth.
        """
        code = compile(source, f"<dynamic:{name}>", "exec")
        module_globals: dict[str, Any] = {
            "__name__": f"_agent_tools.{name}_{content_hash}",
            "__builtins__": {
                **RESTRICTED_BUILTINS,
                "__import__": _make_restricted_import(),
            },
        }
        exec(code, module_globals)  # noqa: S102 — sandboxed source
        return module_globals

    def _handle_collision(self, name: str) -> bool:
        """Apply the configured collision policy. Returns True if the
        caller should overwrite, False if the prior tool stays."""
        policy = self._on_collision
        if policy == "error":
            raise ToolError(
                code="DYNAMIC_TOOL_COLLISION",
                message=f"Tool name {name!r} already registered",
                details={"name": name},
            )
        if policy == "warn":
            _loader_logger.warning("Dynamic tool name collision — replacing %r", name)
            return True
        if policy == "ignore":
            _loader_logger.debug("Dynamic tool name collision — keeping prior %r", name)
            return False
        # "replace" — silent overwrite
        return True

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(event, payload)
        except Exception:
            _loader_logger.exception("dynamic_tool audit sink raised; continuing")


# --- Sandboxed __import__ -------------------------------------------------

# Narrow whitelist — the ONLY modules dynamic tools may import at
# runtime. The decorator provides the ``@tool`` metadata hook; typing
# is needed for type hints to survive ``from __future__ import
# annotations``; dataclasses and ``collections.abc`` support safe
# primitives without exposing side-effectful surfaces.
_SANDBOX_IMPORT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "arcagent.tools._decorator",
        "typing",
        "dataclasses",
        "collections.abc",
    }
)


def _make_restricted_import() -> Callable[..., Any]:
    """Return an ``__import__`` replacement that refuses non-allowlist modules.

    Even when the AST validator is bypassed (new CVE class, missed
    attribute traversal), runtime import attempts for disallowed
    modules raise :class:`ASTValidationError`. The whitelist is narrow
    by design — dynamic tools don't need general library access to do
    useful work.
    """
    real_import = _builtins.__import__

    def _restricted_import(
        name: str,
        globals: dict[str, Any] | None = None,  # noqa: A002 — mirrors builtin signature
        locals: dict[str, Any] | None = None,  # noqa: A002
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name not in _SANDBOX_IMPORT_ALLOWLIST:
            raise ASTValidationError(
                category=f"import:{name}",
                detail=(
                    f"runtime import of {name!r} is blocked in dynamic tools; "
                    f"only {sorted(_SANDBOX_IMPORT_ALLOWLIST)} are allowed"
                ),
            )
        return real_import(name, globals, locals, fromlist, level)

    return _restricted_import


def _find_decorated_tool(
    module_globals: dict[str, Any],
) -> tuple[Callable[..., Any] | None, Any]:
    """Scan the compiled module's globals for a ``@tool``-decorated fn.

    Returns ``(callable, metadata)``, either of which may be ``None``
    if no decorated tool is found.
    """
    for value in module_globals.values():
        meta = getattr(value, "_arc_capability_meta", None)
        if meta is not None and callable(value):
            return value, meta
    return None, None


__all__ = [
    "RESTRICTED_BUILTINS",
    "ASTValidationError",
    "AstValidationCache",
    "AstValidator",
    "CollisionPolicy",
    "DynamicToolLoader",
]
