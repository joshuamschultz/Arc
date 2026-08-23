# Arc Build Standards

> Build like a top 1% developer. No shortcuts. Root causes, not workarounds.

---

## MCP Tools: code-review-graph

This project has a knowledge graph. **Always use code-review-graph MCP tools before Grep/Glob/Read** to explore the codebase. The graph is faster, cheaper, and gives structural context (callers, dependents, coverage) that file scanning cannot. Fall back to Grep/Glob/Read only when the graph doesn't cover what you need.

| Need | Prefer |
|------|--------|
| Exploring code | `semantic_search_nodes` or `query_graph` |
| Blast radius | `get_impact_radius` |
| Code review | `detect_changes` + `get_review_context` |
| Relationships | `query_graph` (`callers_of` / `callees_of` / `imports_of` / `tests_for`) |
| Architecture | `get_architecture_overview` + `list_communities` |
| Affected flows | `get_affected_flows` |
| Renames / dead code | `refactor_tool` |

**Workflow:** graph auto-updates on file changes → `detect_changes` for review → `get_affected_flows` for impact → `query_graph` `tests_for` for coverage.

---

## Coding Principles

@.claude/coding-principles/build-principles.md

@.claude/coding-principles/project-layout.md

@.claude/coding-principles/stack-quality.md

@.claude/coding-principles/threat-surface.md

---

## Compliance

Must support authorization under:

- **FedRAMP** — Federal Risk and Authorization Management
- **NIST 800-53** — Security and Privacy Controls (IA, AU, AC families)
- **CMMC** — Cybersecurity Maturity Model Certification

Evaluate every architectural decision through these frameworks and the OWASP threat surfaces above.