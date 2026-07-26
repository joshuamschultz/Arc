# ADR-021: Agent Self-Description via Optional `[ui]` Section in `arcagent.toml`

**Status**: Accepted
**Date**: 2026-04-29
**Spec**: SPEC-022 ArcUI Agents + Agent Detail with Live Updates (D-003)

## Context

SPEC-022 needs UI hints per agent — display name, color band, role label, hidden-from-fleet flag — so the Agents fleet page renders distinguishable cards and the Agent Detail header shows something more user-friendly than the raw agent_id.

Two viable shapes:

1. **A sidecar directory** `team/<agent>/.arcui/display.toml` (or `.json`). Pros: keeps agent-domain config separate from UI-domain config. Cons: introduces a new file format and parser surface; adds a "did you sync the sidecar?" failure mode; agents that get cloned/forked would have to be aware of the sidecar; another thing to back up.

2. **An optional `[ui]` section** in the existing `arcagent.toml`. Pros: agent self-describes in the file the agent already owns; one source of truth for everything about an agent; cloning the agent dir clones the UI hints; defaults apply silently when `[ui]` is absent. Cons: mixes "what the agent runs" with "how the agent looks." But these are not really separable — the role label *is* part of how the agent identifies itself.

(2) wins on Pillar 1 (Simplicity — fewer files, fewer formats) and Pillar 2 (Modularity — ownership lives with the entity it describes).

## Decision

Agents declare their own UI display hints via an optional `[ui]` block in `arcagent.toml`:

```toml
[agent]
name = "alpha"
type = "scout"

[ui]
display_name = "Alpha (Scout)"   # optional, defaults to agent.name
color = "#3a82f6"                # optional, deterministic hash if absent
role_label = "Reconnaissance"    # optional, defaults to agent.type
hidden = false                   # optional, default false
```

Parsing lives in a new module `arcgateway.agent_config` (NOT extension to `arcgateway.config`, which is the gateway *daemon's* own `gateway.toml` schema — see decision log D-022-A). `load_ui_section(toml_dict) -> UISection` is a pure function: dict in, dataclass out. Missing fields get sane defaults; wrong-type fields silently fall through to defaults rather than failing the agent's boot. UI hints are advisory — they should never break an agent.

`arcgateway.team_roster.list_team()` reads `[ui]` per agent and stamps:
- `display_name` (falls back to `agent.name`)
- `color` (falls back to `_deterministic_color(agent_id)` — sha256-derived hex)
- `role_label` (falls back to `agent.type` or empty string)
- `hidden` (default `False`)

Hidden agents are excluded from the fleet card grid but still routable via deep-link `?page=agent-detail&agent=<id>`.

## Consequences

### Positive

- **One file per agent** continues to be the truth. No sidecar to forget. Cloning `team/alpha_agent/` clones the UI hints.
- **Backward compatible by absence**. Existing agents (no `[ui]` section) get sensible defaults — every field falls through.
- **No new file format**. The TOML parser arcui already loads handles this.
- **Schema-light**. Optional fields, no required validation. Wrong-type fields silently use defaults — UI hints are not safety-critical, so failing soft is correct.

### Negative

- **arcagent.toml grows**. We accepted this in advance — the file is the agent's source of truth and config files are allowed to grow when the data is ontologically about the agent.
- **No type-strict validation today**. A user typing `color = 12345` (int instead of str) gets the deterministic-hash default, not an error. We chose graceful degradation over loud failure because UI display is not mission-critical.

### Out of scope

- Theming the entire agent detail page from `[ui]` (custom CSS, layout, etc.). Single-color band + display name is enough for Phase 1.
- Editing `[ui]` from the UI. Read-only by design — write paths to agent config are not in SPEC-022 scope.

## References

- SPEC-022 README §"Adds" item 5 (`load_ui_section`)
- SPEC-022 SDD §4.4 (`team_roster.list_team` consumes UI hints)
- SPEC-022 SDD §4.5 (`config` extension, superseded by D-022-A → `agent_config.py`)
- SPEC-022 README §"Decision Log" D-022-A
- ADR-020 (arcgateway as data plane — `agent_config` lives in gateway because it's part of the read surface)
