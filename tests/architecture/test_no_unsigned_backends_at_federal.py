"""Architecture test: backend imports must be gated by signature verify.

Assertion (post §8.13 / Phase C lockdown — supply-chain hardened at all tiers)
------------------------------------------------------------------------------
Inside ``arcrun.backends.loader.load_backend``, when a non-builtin backend is
requested, the loader must call ``verify_allowed_backends_signature`` (or its
underscore alias ``_verify_allowed_backends_signature``) BEFORE any third-party
module import.

Phase C history: the gate was originally scoped to ``if tier == "federal":``,
but §8.13 elevated it to apply at **all tiers** for non-builtins. The
``federal=`` kwarg is now metadata to the verifier (controls which issuers are
trusted), not a guard on whether to verify. This test reflects the stronger
post-§8.13 invariant.

We enforce this by:

1. Walking the AST of ``loader.py``.
2. Finding the ``load_backend`` FunctionDef.
3. Confirming that across its whole body, a call to one of the gate functions
   appears before any call to ``_load_dotted`` or ``importlib.import_module``.

If a future refactor reorders these calls, this test fails LOUDLY so no
silent regression opens the door to unsigned third-party backend imports.

Rationale
---------
OWASP LLM03 (Supply Chain) + ASI04 (Agentic Supply Chain). The trust model
assumes every backend wheel loaded into the agent process has been reviewed and
signed by a trust authority. Losing this gate means a compromised PyPI mirror
or typosquatted package can run code in the agent's process.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Both the canonical name and the test-import alias defined at module bottom.
# Post-§8.13 the gate function lives in _verifier.py and is re-exported via
# both a direct ``from … import …`` and an ``_underscore`` assignment in
# loader.py. Either reference inside load_backend() satisfies the gate.
_GATE_FUNCTIONS = {
    "verify_allowed_backends_signature",
    "_verify_allowed_backends_signature",
    # Legacy names preserved for forward-compat with any future revert:
    "_enforce_federal_manifest",
}

_IMPORT_CALLS = {
    "_load_dotted",
    "_try_entry_point",
    "import_module",  # importlib.import_module
}


def _load_loader_module_source() -> ast.Module:
    repo_root = Path(__file__).parents[2]  # Arc/
    loader_path = repo_root / "packages" / "arcrun" / "src" / "arcrun" / "backends" / "loader.py"
    assert loader_path.exists(), f"Expected loader at {loader_path}"
    return ast.parse(loader_path.read_text(encoding="utf-8"), filename=str(loader_path))


def _extract_call_name(call: ast.Call) -> str | None:
    """Return the callable's simple name if we can determine it, else None."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Did not find {name}() in loader.py")


def _calls_in_order(node: ast.AST) -> list[tuple[str, int]]:
    """Return (callable_name, lineno) tuples in source order within `node`."""
    calls: list[tuple[str, int]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = _extract_call_name(sub)
            if name is not None:
                calls.append((name, sub.lineno))
    calls.sort(key=lambda kv: kv[1])
    return calls


def _gate_names_in_scope(tree: ast.Module) -> set[str]:
    """Names from _GATE_FUNCTIONS that resolve to *something* in loader.py.

    Accepts any of:
      - a FunctionDef of that name defined locally,
      - an ``from … import name`` (with or without ``as`` alias),
      - an ``_alias = original`` assignment (the §8.13 re-export pattern).
    """
    resolved: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _GATE_FUNCTIONS:
            resolved.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in _GATE_FUNCTIONS:
                    resolved.add(bound)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _GATE_FUNCTIONS:
                    resolved.add(target.id)

    return resolved


def test_load_backend_calls_gate_before_any_import() -> None:
    """The signature gate MUST run before any third-party backend import.

    Post-§8.13: the gate applies to **all** non-builtin loads (the federal/
    enterprise/personal distinction is now passed *to* the verifier, not used
    *as* a guard on whether to verify).
    """
    tree = _load_loader_module_source()
    load_backend = _find_function(tree, "load_backend")

    calls = _calls_in_order(load_backend)
    assert calls, "load_backend has no calls at all — refactor?"

    gate_index = next(
        (i for i, (n, _) in enumerate(calls) if n in _GATE_FUNCTIONS),
        None,
    )
    assert gate_index is not None, (
        "load_backend() does not call a signature-gate function.  "
        f"Expected one of: {_GATE_FUNCTIONS}.  Got calls: {calls}"
    )

    for i, (name, _) in enumerate(calls):
        if name in _IMPORT_CALLS:
            assert i > gate_index, (
                f"Found third-party import call {name!r} at position {i} "
                f"BEFORE the signature gate at position {gate_index}.  "
                "Manifest signature must be verified before importing any "
                f"non-built-in backend.  Full call order: {calls}"
            )


def test_load_backend_requires_tier_param() -> None:
    """Sanity: load_backend() must accept ``tier`` as a kw-only parameter.

    The tier value is passed *to* the verifier to select which issuer set is
    trusted; without it the verify call cannot make a tier-appropriate trust
    decision.
    """
    tree = _load_loader_module_source()
    load_backend = _find_function(tree, "load_backend")
    kwonly_names = {arg.arg for arg in load_backend.args.kwonlyargs}
    assert "tier" in kwonly_names, (
        f"load_backend signature changed — tier is not kw-only.  kwonly args: {kwonly_names}"
    )


def test_gate_function_resolvable_in_loader_scope() -> None:
    """Belt-and-suspenders: the gate name in load_backend must actually resolve.

    Guards against someone deleting the gate import / alias without also
    removing the call site (Python would raise NameError at runtime; we want a
    static check too).

    Accepts the §8.13 pattern where the gate is imported from _verifier.py and
    optionally re-exported as ``_verify_allowed_backends_signature`` for tests.
    """
    tree = _load_loader_module_source()

    resolvable = _gate_names_in_scope(tree)
    assert resolvable, (
        f"None of the gate names {_GATE_FUNCTIONS} are reachable from "
        "loader.py via FunctionDef, ImportFrom, or alias assignment."
    )

    load_backend = _find_function(tree, "load_backend")
    called = {_extract_call_name(n) for n in ast.walk(load_backend) if isinstance(n, ast.Call)}
    assert called & resolvable, (
        f"load_backend() does not call any of the resolvable gate names "
        f"{resolvable}.  Calls observed: {called}"
    )
