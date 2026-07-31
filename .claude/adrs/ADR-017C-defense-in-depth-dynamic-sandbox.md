---
id: ADR-017C
title: Defense-in-Depth for Dynamic Tool Sandbox
status: accepted
date: 2026-04-18
spec: SPEC-017
tags:
  - security
  - sandbox
  - defense-in-depth
  - ast-validation
  - cve-mitigation
---

# ADR-017C: Defense-in-Depth for Dynamic Tool Sandbox

## Status

Accepted (2026-04-18). Discovered during `/review` pass, fixed before merge.

## Context

The initial SPEC-017 Phase 7 implementation of `DynamicToolLoader._compile_in_sandbox` built the execution namespace as:

```python
module_globals["__builtins__"] = {
    **RESTRICTED_BUILTINS,
    "__import__": _builtins.__import__,  # full import power
}
```

The reasoning: the AST validator rejects `import os`, `import ctypes`, etc. statically, so runtime exposure of `__import__` is "safe enough".

**Review caught this as a defense-in-depth violation.** Evidence:

- RestrictedPython has shipped 3 CVEs in the last 2 years where the AST walker missed a bypass class (CVE-2023-37271 generator frame traversal, CVE-2025-22153 try/except*, CVE-2024-47532 AttributeError obj leak).
- A single missed pattern = full sandbox escape because runtime `__import__` is unconstrained.
- Python's reflection surface is vast. Blocking every path via AST alone is brittle by design.

## Decision

Wrap `__import__` with `_make_restricted_import()` that enforces a **narrow whitelist** of permitted modules:

```python
_SANDBOX_IMPORT_ALLOWLIST = frozenset({
    "arcagent.tools._decorator",  # the @tool hook
    "typing",                     # for type hints
    "dataclasses",                # safe primitives
    "collections.abc",            # Protocol / Callable etc.
})
```

Any import attempt at runtime against a non-allowlisted module raises `ASTValidationError` with category `import:{name}`. This means:

- AST validator catches known bypass patterns at static-analysis time
- Restricted builtins dict blocks access to `__import__` via builtins
- Wrapped `__import__` catches anything that still reaches runtime
- Network egress is gated separately via `ToolContext.http`

**Four independent layers must all fail** for a sandbox escape. The AST validator being the slowest-moving (hardest to update for new CVEs) is mitigated by runtime layers that need no updates.

## Consequences

### Positive

- **No single-bypass escape** — even a future CVE against the AST validator doesn't yield sandbox escape.
- **Runtime defense is simple** — 15 lines of code, trivial to audit, no regex or heuristics.
- **Aligns with NIST 800-53 SI-7(15)** — integrity checks on executing code at multiple layers.

### Negative

- **Dynamic tools can't use arbitrary Python libraries.** The whitelist is narrow (4 modules) by intent.
- **Expanding the whitelist requires security review.** Each new module is a potential re-introduction of the problem.

### Mitigations

- The 4 whitelisted modules cover the genuine needs: `@tool` decoration, type hints surviving `from __future__ import annotations`, dataclasses for return types, Protocol/Callable for Optional callbacks.
- Tools that need richer behavior should be shipped as extensions (signed, module-authored) rather than dynamic tools.

## Adversarial tests

Two regression tests guard this:

```python
def test_runtime_os_import_blocked_even_if_ast_validator_missed_it():
    # Construct namespace directly, bypass AST, verify runtime refuses
    ...

def test_runtime_subprocess_import_blocked():
    ...
```

These simulate the "AST bypass exists" scenario by constructing the execution namespace directly and confirming the wrapped `__import__` still refuses.

## Alternatives considered

- **Keep full `__import__`, trust AST validator** — rejected. History shows AST validators are a single point of failure.
- **Remove `__import__` entirely** — rejected. Tools need `from arcagent.tools._decorator import tool` to declare themselves.
- **Block only specific modules (blocklist)** — rejected. Open-by-default; any missed module = escape. Allowlist inverts this.
- **Subprocess sandbox (Firecracker microVM)** — considered for future. Orthogonal; adds OS-level isolation but doesn't obviate in-process defense.

## References

- SPEC-017 R-052, R-053, R-054
- CVE-2023-37271, CVE-2024-47532, CVE-2025-22153 (RestrictedPython history)
- CVE-2025-68668 (n8n Pyodide ctypes FFI escape)
- arXiv:2603.15973 — non-compositional safety
- NIST 800-53 SI-7(15) — integrity checks on executing code
