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
import enum
import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.core.errors import ArcAgentError, ToolError

# --- Import policy --------------------------------------------------------

# The enterprise blocklist, grouped for self-documenting errors. Four groups
# of privileged modules an agent-authored tool must not import at enterprise
# tier (operator ``allow_imports`` entries are subtracted as exceptions):
#   filesystem   — read/write outside the workspace boundary
#   process/exec — spawn processes / bypass the isolation backend
#   interpreter  — reach the interpreter internals / (de)serialize arbitrary objects
#   network      — open outbound sockets outside the audited egress proxy
_ENTERPRISE_BLOCKED_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("filesystem", frozenset({"os", "shutil", "pathlib", "tempfile", "glob"})),
    ("process/exec", frozenset({"subprocess", "multiprocessing"})),
    ("interpreter", frozenset({"sys", "ctypes", "importlib", "pickle", "marshal", "shelve"})),
    ("network", frozenset({"socket", "urllib", "http", "requests", "httpx"})),
)

# Minimal always-allowed set at federal (pure allowlist) so the @tool decorator
# import and ``from __future__ import annotations`` always validate — without it
# no tool could ever be authored at federal.
_FEDERAL_SEED_IMPORTS: frozenset[str] = frozenset({"__future__", "arcagent"})


class ImportMode(enum.Enum):
    """How :class:`ImportPolicy` decides whether a module import is permitted."""

    ALLOW_ALL = "allow_all"  # personal (or an enterprise blanket opt-out)
    BLOCKLIST = "blocklist"  # enterprise: allow most, block the four groups
    ALLOWLIST = "allowlist"  # federal: deny by default, only listed modules pass


@dataclass(frozen=True)
class ImportPolicy:
    """Single source of truth for which module imports agent-authored tools may use.

    Shared by the authoring gate (``create_tool``/``update_tool``) and the load
    gate (:class:`CapabilityLoader`) so a tool refused at authoring is never one
    the loader would have run, and vice-versa. Only *module imports* are
    policy-relaxable; sandbox-escape checks (eval/exec, frame traversal, blocked
    attributes, encoding) are always enforced by :class:`AstValidator`.

    Construct via :func:`resolve_workspace_import_policy`, never directly.
    """

    mode: ImportMode
    tier: str
    # ALLOWLIST mode: the exact set of permitted top-level modules.
    allowed: frozenset[str] = field(default_factory=frozenset)
    # BLOCKLIST mode: operator exceptions subtracted from the blocked groups.
    exceptions: frozenset[str] = field(default_factory=frozenset)

    def allows(self, module: str) -> bool:
        """Return True if ``module`` (dotted path) may be imported."""
        top = module.split(".", 1)[0]
        if self.mode is ImportMode.ALLOW_ALL:
            return True
        if self.mode is ImportMode.ALLOWLIST:
            return top in self.allowed
        return top not in self._blocked_modules()

    def _blocked_modules(self) -> frozenset[str]:
        blocked: set[str] = set()
        for _group, modules in _ENTERPRISE_BLOCKED_GROUPS:
            blocked |= modules
        return frozenset(blocked - self.exceptions)

    def describe(self) -> str:
        """One-line, human-readable description of the effective policy.

        The single place the policy prose lives, so an authoring-rejection error
        (which the agent cannot supplement by reading files outside its
        workspace) can teach the rule inline.
        """
        if self.mode is ImportMode.ALLOW_ALL:
            return "all imports are permitted"
        if self.mode is ImportMode.ALLOWLIST:
            allowed = ", ".join(sorted(self.allowed)) or "(none)"
            return f"only these imports are permitted: {allowed}"
        groups = "; ".join(
            f"{name} ({', '.join(sorted(modules - self.exceptions))})"
            for name, modules in _ENTERPRISE_BLOCKED_GROUPS
            if modules - self.exceptions
        )
        exc = ", ".join(sorted(self.exceptions)) or "(none)"
        return f"blocked import groups: {groups}; exceptions configured: {exc}"


# Bare ``AstValidator()`` / unconfigured runtime default: enterprise blocklist
# with no exceptions — fail-closed, so a misconfigured caller never silently
# gets allow-all.
DEFAULT_IMPORT_POLICY: ImportPolicy = ImportPolicy(mode=ImportMode.BLOCKLIST, tier="enterprise")

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


# Single-place guidance appended to every authoring rejection: the agent must
# author tools only via the signing tools, or the artifact is denied at load.
_AUTHORING_GUIDANCE = (
    "Author tools only via create_tool/update_tool — they sign the artifact; "
    "files written any other way are unsigned and will be denied at load (TOFU)."
)


def format_authoring_rejection(exc: ASTValidationError, policy: ImportPolicy) -> str:
    """Build a self-documenting rejection string for create_tool/update_tool.

    Names the resolved tier, the specific violation, the effective policy
    (via :meth:`ImportPolicy.describe`), and the authoring guidance — the agent
    cannot read policy files outside its workspace, so the rule is taught inline.
    """
    return (
        f"Error: AST validation rejected source — {exc}. "
        f"Tier {policy.tier}: {policy.describe()}. {_AUTHORING_GUIDANCE}"
    )


# --- Validator ------------------------------------------------------------


class AstValidator(ast.NodeVisitor):
    """Walks a parsed AST and rejects prohibited patterns.

    Stateful — a single instance tracks the first violation seen and
    raises immediately. Construct fresh per source file.
    """

    def __init__(self, *, policy: ImportPolicy = DEFAULT_IMPORT_POLICY) -> None:
        self._violation: tuple[str, str] | None = None
        # Tier-resolved import policy (single source of truth). Default is the
        # fail-closed enterprise blocklist, so a bare validator never silently
        # allows all imports. Only module imports are policy-relaxable;
        # eval/exec/frame-traversal stay blocked unconditionally.
        self._policy = policy
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
        if self._policy.allows(module):
            return
        top = module.split(".", 1)[0]
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


def resolve_workspace_import_policy(
    tier: str,
    *,
    allow_all_imports: bool,
    allow_imports: list[str],
) -> ImportPolicy:
    """Resolve the effective :class:`ImportPolicy` for workspace-authored tools.

    - personal:   allow-all — every import passes.
    - enterprise: blocklist — allow most, block the four privileged groups;
                  ``allow_imports`` entries are subtracted as operator
                  exceptions; ``allow_all_imports=True`` is honored as a blanket
                  operator opt-out (allow-all).
    - federal:    pure allowlist (deny by default) — ONLY ``allow_imports`` (plus
                  the minimal always-allowed seed ``__future__``/``arcagent``)
                  passes; ``allow_all_imports`` is IGNORED (no blanket relaxation).

    Any unknown tier falls toward the stricter enterprise blocklist. Sandbox-escape
    protections (eval/exec, frame traversal) are unaffected — this governs module
    imports only.
    """
    if tier == "personal":
        return ImportPolicy(mode=ImportMode.ALLOW_ALL, tier="personal")
    if tier == "federal":
        return ImportPolicy(
            mode=ImportMode.ALLOWLIST,
            tier="federal",
            allowed=_FEDERAL_SEED_IMPORTS | frozenset(allow_imports),
        )
    resolved_tier = tier if tier == "enterprise" else "enterprise"
    if allow_all_imports:
        return ImportPolicy(mode=ImportMode.ALLOW_ALL, tier=resolved_tier)
    return ImportPolicy(
        mode=ImportMode.BLOCKLIST, tier=resolved_tier, exceptions=frozenset(allow_imports)
    )


class AstValidationCache:
    """Skip re-validation of unchanged files (R-001 perf gate).

    Keyed by absolute path → ``(md5, mtime)``. A second call with the
    same content + same mtime returns immediately. Either an mtime
    bump or a content hash change forces re-validation. Validation
    failures do NOT populate the cache, so the next attempt re-runs
    and re-raises (no false positives, no silent passes).
    """

    def __init__(self, *, policy: ImportPolicy = DEFAULT_IMPORT_POLICY) -> None:
        self._entries: dict[Path, tuple[str, float]] = {}
        self._policy = policy

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

        _self.AstValidator(policy=self._policy).validate(source)
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


def _egress_accessor() -> Any:
    """Return the per-agent EgressProxy — the sandbox's ONLY outbound path.

    Injected as the bare name ``egress`` into the restricted namespace so
    agent-authored source reaches the network only through the allowlist-gated,
    audited proxy (SPEC-017 R-055 / SPEC-035 REQ-013). This is the real caller
    of :func:`_runtime.egress`; direct sockets/imports are blocked by the AST
    gate and restricted ``__import__``. Raises when no proxy is wired — outbound
    network is denied by absence, not silently no-op.
    """
    from arcagent.builtins.capabilities import _runtime

    proxy = _runtime.egress()
    if proxy is None:
        raise ToolError(
            code="EGRESS_UNAVAILABLE",
            message="No egress proxy is configured; outbound network is disabled",
            details={},
        )
    return proxy


def build_restricted_builtins(
    *, policy: ImportPolicy = DEFAULT_IMPORT_POLICY
) -> dict[str, object]:
    """Build a ``__builtins__`` dict for executing agent-authored module source.

    RESTRICTED_BUILTINS (no ``open``/``eval``/``exec``) plus ``__build_class__``
    (so class-based capabilities can be defined) and a runtime ``__import__``
    that mirrors the AST import gate — the same modules the static gate rejects
    under ``policy`` are refused at runtime too.

    This is the hardened namespace the capability loader uses in place of a
    bare ``exec(code, module.__dict__)``. It is defense-in-depth / a fast-fail
    linter in front of the SPEC-036 execution sandbox — never a substitute for
    it (the object graph is escapable; real isolation is the sandbox's job).
    """
    return {
        **RESTRICTED_BUILTINS,
        "__build_class__": _builtins.__build_class__,
        "egress": _egress_accessor,
        "__import__": _make_policy_import(policy),
    }


def _make_policy_import(policy: ImportPolicy) -> Callable[..., Any]:
    """Return an ``__import__`` refusing modules the ``policy`` denies.

    Mirrors the AST validator's policy so multi-capability workspace files may
    still use permitted modules (``json``/``re``/...) while denied ones are
    refused at runtime.
    """
    real_import = _builtins.__import__

    def _restricted(
        name: str,
        globals: dict[str, Any] | None = None,  # noqa: A002 — mirrors builtin signature
        locals: dict[str, Any] | None = None,  # noqa: A002
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if not policy.allows(name):
            top = name.split(".", 1)[0]
            raise ASTValidationError(
                category="import:" + top,
                detail=f"runtime import of {name!r} is blocked in workspace source",
            )
        return real_import(name, globals, locals, fromlist, level)

    return _restricted


__all__ = [
    "DEFAULT_IMPORT_POLICY",
    "RESTRICTED_BUILTINS",
    "ASTValidationError",
    "AstValidationCache",
    "AstValidator",
    "ImportMode",
    "ImportPolicy",
    "build_restricted_builtins",
    "format_authoring_rejection",
    "resolve_workspace_import_policy",
]
