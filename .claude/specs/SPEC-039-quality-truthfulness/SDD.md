# SPEC-039 SDD — Quality / Truthfulness Pass (code)

## Item 1 — core-slim (REQ-001)
Budget script globs `packages/arcagent/src/arcagent/core/*.py` non-recursively.
Relocate `core/tool_transport.py` (158 NCLOC) → `tools/_transport.py`. It is pure
tool-definition primitives (ToolTransport enum, RegisteredTool dataclass, `native_tool`
decorator, `_validate_tool_args`, `_echo_tool`) — tool-domain, not nucleus orchestration.
`core/tool_registry.py` is the only direct importer; it re-exports every moved name, so
external imports are unchanged. `core → tools` is already an established edge
(tool_registry imports `tools/_policy_fill`, `tools/human_gate`).
Result: core 3569 → 3411 (89 margin). Behavior-preserving.

Rejected candidates:
- `agent.py` helpers → `agent_dispatch.py`: both are core files → net-zero for the budget.
- `config.py` blocks → `modules/`: would invert layering (core importing modules).

## Item 2 — arcskill mypy gate (REQ-002)
Add `[tool.mypy]` (strict, matching repo convention) to `arcskill/pyproject.toml` with a
module override `ignore_missing_imports` for the optional `sigstore` / `sigstore.*` extras.
Fix `_DockerBackend` attr-defined by adding `__all__` to `hub/_docker.py` (explicit
re-export of the aliased optional import). Remove two genuinely-unused `type: ignore` on
`import yaml` (types-PyYAML present).

## Item 3 — conftest collision (REQ-003)
Root cause: empty `tests/__init__.py` in arctrust and arccli make both conftests resolve to
the same top-level module `tests.conftest`, aborting a cross-package collection. Fix: delete
the two empty `__init__.py`. Safe because both test dirs are flat and neither suite uses
`import tests…`. arcagent is unaffected (its conftest is package-root, module
`packages.arcagent.conftest`).

## Item 4 — per-tier default ceilings (REQ-004)
In `tools/_policy_fill.py` add conservative per-tier default constants and a single
`_effective_ceilings(config)` helper that fills unset ceilings from the tier default
(federal tightest, enterprise looser, personal absent → unbounded). `resolve_run_budget`
and `resolve_provider_limits` both route through it, so the arcrun circuit-breaker and the
arctrust ProviderLayer are default-on at federal/enterprise. Operator-set values win.

Defaults: federal {tokens 500k, cost $10, req 500}; enterprise {tokens 2M, cost $50, req 2k}.

## Collateral fix
`arccli/tests/test_spec017_cli.py::test_federal_lists_all_five` asserted a stale 6-layer
federal list; SPEC-038 inserted the `classification` layer. Updated the assertion to the
real 7-layer list and renamed the test to `test_federal_lists_all_layers` (was pre-existing
red on develop; fixed per the "leave it correct" rule).
</content>
