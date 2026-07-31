# Spec 014 — Arc CLI

## Metadata

| Field | Value |
|-------|-------|
| Spec ID | 014 |
| Feature | Unified Arc CLI (`arc` command) |
| Status | COMPLETE |
| Created | 2026-02-14 |
| Author | Josh + Claude |

## Documents

| Document | Purpose |
|----------|---------|
| [PRD.md](PRD.md) | Problem, goals, requirements, user stories |
| [SDD.md](SDD.md) | Design, components, ADRs, edge cases |
| [PLAN.md](PLAN.md) | Phased tasks with checkboxes and acceptance criteria |

## Decisions Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-200 | Separate repo at `~/AI/arccli/` | Own project outside arcllm. arccli depends on arcllm (and future arc products) as external deps. Lives at same level as arcllm, arcrun, arcagent in ~/AI/. |
| D-201 | Click framework for CLI | Most popular Python CLI framework. Decorator-based, great docs, handles complex nested commands. Used by Flask, pip, AWS CLI. Scales well for future arc products. |
| D-202 | `arc` as root command with git-style subcommands | Single entry point: `arc llm call`, `arc llm providers`. Clean namespace, one command to remember. Extensible to `arc run`, `arc agent`. |
| D-203 | Human-readable tables by default, `--json` flag | Tables for humans, JSON for scripts/piping. Best of both worlds. |
| D-204 | 7 commands for v1 | config, providers, provider, models, call, validate, version. Covers config inspection, provider/model discovery, calling, validation. Log/history viewing deferred until storage backends exist. |

## Cross-References

- Depends on: arcllm (external dependency)
- Location: `~/AI/arccli/` (separate project, same level as `~/AI/arcllm/`)
