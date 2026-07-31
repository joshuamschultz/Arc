# ADR-004: Core LOC Budget Increase to 3,500

**Status**: Accepted
**Date**: 2026-02-15
**Decision Makers**: Josh Schultz
**Relates to**: S003 Phase 1b Review, CLAUDE.md Build Standards

---

## Context

CLAUDE.md mandates "Core stays under 3,000 LOC. Period." After S003 Phase 1b implementation (extensions, session manager, settings manager, skill registry), core/ contains 3,239 LOC across 14 files:

| File | LOC | Purpose |
|------|-----|---------|
| agent.py | 563 | Orchestrator |
| extensions.py | 434 | Extension system (new in 1b) |
| config.py | 304 | TOML config, Pydantic models |
| tool_registry.py | 284 | Tool registry, 4 transports |
| session_manager.py | 257 | Multi-turn sessions (new in 1b) |
| context_manager.py | 226 | Context management |
| identity.py | 210 | DID, keypairs |
| module_bus.py | 204 | Event bus |
| errors.py | 203 | Error hierarchy |
| settings_manager.py | 197 | Runtime settings (new in 1b) |
| skill_registry.py | 161 | Skill discovery (new in 1b) |
| telemetry.py | 143 | OpenTelemetry, audit |
| protocols.py | 52 | Protocol definitions |
| __init__.py | 1 | Package marker |

The 3,000 LOC budget was set during initial architecture planning (S001) when core/ had 6 components. Phase 1b added 4 more components (extensions, session_manager, settings_manager, skill_registry) totaling ~1,049 LOC, pushing the total to 3,239.

## Decision

**Increase the core LOC budget from 3,000 to 3,500.** The original budget was set before the full component inventory was known. The 8% overage reflects legitimate complexity from security-hardened extension loading, session management with JSONL persistence, and TOML-backed runtime settings.

## Alternatives Considered

### 1. Extract modules from core/

Move session_manager, settings_manager, or skill_registry to `arcagent/modules/`. Rejected because:
- These are nucleus components — they wire directly into agent.py's startup/shutdown lifecycle
- Moving them creates artificial package boundaries and circular import risks
- The module bus pattern (modules/) is for optional, independently-loadable extensions

### 2. Aggressively reduce LOC

Collapse error hierarchy, remove docstrings, compress config models. Rejected because:
- Explicit error codes are critical for observability in federal environments
- Docstrings are required per build standards ("Comment completely at module, class, and non-obvious method level")
- Config models need explicit Pydantic fields for validation and documentation

### 3. Keep the 3,000 limit

Accept the overage as tech debt and plan extraction. Rejected because:
- The overage is small (8%) and reflects real, tested functionality
- Artificially constraining the nucleus would push security-critical code (sandbox, path validation) out of the core review surface
- Better to acknowledge reality and set a meaningful budget

## Rationale

1. **Component count grew legitimately.** Phase 1b added 4 components that are genuinely nucleus-level: they initialize during startup, participate in shutdown, and are directly referenced by agent.py.

2. **LOC per component is reasonable.** Average 231 LOC/component. No single file exceeds 563 LOC.

3. **The budget still constrains.** 3,500 is tight enough to prevent bloat but realistic for 14 components averaging ~250 LOC each.

4. **Security code should stay in core.** The extension sandbox (434 LOC) handles security-critical code like `_strict_sandbox()`, module signature verification points, and entry point validation. This belongs in the nucleus where it gets maximum review attention.

## Consequences

### Positive
- Honest accounting of core complexity
- No artificial module extraction that creates import complexity
- Clear budget for Phase 2 planning (261 LOC remaining for new core features)

### Negative
- Relaxed constraint could invite LOC creep
- Phase 2 features (MCP transport, NATS bus) will need careful budgeting

### Mitigations
- Monitor LOC at each phase boundary
- If core exceeds 3,500, extraction is mandatory (not optional)
- Consider promoting extensions.py to `arcagent/extensions/` package if it grows further
- Track in tech-debt.json with priority threshold
