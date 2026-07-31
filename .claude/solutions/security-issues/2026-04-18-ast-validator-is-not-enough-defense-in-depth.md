---
title: "AST Validator Is Not Enough — Defense-in-Depth for Dynamic Code Sandboxes"
category: security-issues
date: 2026-04-18
tags:
  - sandbox
  - defense-in-depth
  - ast-validation
  - dynamic-code
  - cve-mitigation
  - restricted-builtins
  - python-security
module: arcagent.tools._dynamic_loader
symptom: "Review caught a sandbox that relied on AST validation alone to block privileged imports — a single AST bypass = full sandbox escape."
root_cause: "Static validation is inherently incomplete. Python's reflection surface is vast; new bypass classes keep being discovered (RestrictedPython has 3 CVEs in 2 years). Exposing unconstrained __import__ at runtime makes any AST miss catastrophic."
severity: medium
spec_id: SPEC-017
resolution_verified: true
tests_passing: 37
---

# AST Validator Is Not Enough — Defense-in-Depth for Dynamic Code Sandboxes

## When this pattern applies

You are building a sandbox for dynamically-provided Python code —
agent-generated tools, user plugins, embedded scripting, LLM-generated
snippets, anything where the source is untrusted and arrives at
runtime. You have (or are building) an AST walker that rejects
known-bad patterns: `import os`, `__subclasses__`, `gi_frame`, etc.

**If you stop there, you have a critical security gap.**

## The mistake

```python
# BEFORE — single-layer AST defense
AstValidator().validate(source)
compile_and_exec(
    source,
    globals={
        "__builtins__": {**SAFE_BUILTINS, "__import__": _builtins.__import__}
    },
)
```

Rationale that looks fine at review time:

- "The AST validator rejects `import os`, so we never reach runtime with a dangerous import."
- "Exposing `__import__` at runtime is fine because the validator already caught it."
- "We've tested known bypass patterns; they all fail validation."

## Why this fails

**AST walkers are a single point of failure.** Evidence:

| CVE | Component | Bypass class |
|-----|-----------|--------------|
| CVE-2023-37271 | RestrictedPython | Generator `.gi_frame.f_back` chain |
| CVE-2024-47532 | RestrictedPython | `AttributeError.obj` leakage |
| CVE-2025-22153 | RestrictedPython | `try/except*` handler confusion |
| CVE-2025-68668 | n8n Pyodide | `ctypes.CDLL(None).system()` FFI escape |

Each of these was a missed bypass pattern in a mature, well-reviewed
AST walker. Each gave **complete sandbox escape** because runtime had
no independent defense.

Python's reflection surface is the actual problem:

- `__class__`, `__bases__`, `__subclasses__` chains reach importers
- `__reduce__` / `__reduce_ex__` execute arbitrary code on unpickling
- Generator frames expose `f_builtins` → unrestricted `__import__`
- Codec hooks run BEFORE the AST parser (UTF-7 encoding attacks)
- `string.Formatter` traverses attributes
- `ctypes` FFI bypasses every Python-level restriction

You cannot enumerate the whole surface statically. History says you
will miss at least one.

## The solution — four independent layers

```python
# AFTER — defense in depth
AstValidator().validate(source)               # layer 1: static
compile_and_exec(
    source,
    globals={
        "__builtins__": {
            **SAFE_BUILTINS,                  # layer 2: scrubbed builtins
            "__import__": _restricted_import(), # layer 3: runtime allowlist
        },
    },
)
# layer 4: network egress proxy — separate concern
```

Each layer needs ALL the others to fail for a sandbox escape. The
slowest-moving layer (AST validation, which needs updates for new CVE
classes) is backed by layers that don't need updates.

### Key implementation detail: wrapped `__import__`

```python
_SANDBOX_IMPORT_ALLOWLIST = frozenset({
    "arcagent.tools._decorator",  # the @tool hook
    "typing",                     # for type hints
    "dataclasses",                # safe primitives
    "collections.abc",            # Protocol / Callable
})

def _make_restricted_import():
    real_import = _builtins.__import__
    def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name not in _SANDBOX_IMPORT_ALLOWLIST:
            raise ASTValidationError(
                category=f"import:{name}",
                detail=f"runtime import of {name!r} blocked",
            )
        return real_import(name, globals, locals, fromlist, level)
    return _restricted_import
```

## Regression tests that guard it

The adversarial test must **bypass the AST validator** to prove the
runtime defense works:

```python
def test_runtime_os_import_blocked_even_if_ast_validator_missed_it():
    """Construct namespace directly (bypass AST) and verify runtime refuses."""
    ns = {
        "__builtins__": {
            **RESTRICTED_BUILTINS,
            "__import__": _make_restricted_import(),
        }
    }
    src = compile("x = __import__('os')", "<test>", "exec")
    with pytest.raises(ASTValidationError):
        exec(src, ns)
```

Without bypassing the AST, the test can't distinguish "AST caught it"
from "runtime caught it". This is why the test name is explicit about
simulating the missed-bypass scenario.

## Design principle

**For any sandbox: "static validation + runtime enforcement + restricted
capability surface" is a minimum of three independent layers.** Each
layer must fail-closed. Any single layer being bypassed must NOT grant
escape.

## Cost

- Dynamic tools can only import ~4 whitelisted modules.
- Whitelist expansion requires security review.

## Tradeoff

Narrow functionality (no arbitrary library use) in exchange for
resistance to unknown future CVE classes. For any code-executing
sandbox shipped to production, this tradeoff should favor resistance
every time. Code that genuinely needs library access should ship as a
signed extension (supply-chain-verified at load time), not as a
dynamic tool.

## Verification

- 37 adversarial tests across `tests/security/` (AST bypass + restricted builtins + egress)
- 2 new tests specifically for the runtime `__import__` defense
- Coverage on `_dynamic_loader.py`: 94%
- ADR-017C formalizes the decision

## Applicability beyond SPEC-017

This pattern applies to:

- LLM-generated code sandboxes (same threat model)
- Plugin/extension systems that exec user-provided code
- Template engines that allow expression evaluation
- Any embedded scripting surface (even "safe" ones like Jinja with filters)

If your sandbox has a "these patterns are not allowed" static check
AND runtime exposes the full surface behind it, you have this bug.
