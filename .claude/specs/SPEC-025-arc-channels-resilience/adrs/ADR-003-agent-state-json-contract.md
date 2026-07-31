# ADR-003 — `~/.arcagent/agent-state.json` as the arc-stack ↔ arcui contract

**Status:** Accepted (2026-05-06)
**Spec:** SPEC-025 — Track C (deploy manifest + per-agent health)
**Pillar trace:** Simplicity (boring file IPC), Modularity (no new gateway endpoints), Security (umask-protected, freshness-validated)

## Context

SPEC-025 §FR-4 needs each agent's connect status (`connected | degraded`)
visible to arcui's `/api/team/roster` so the UI can show a "degraded" badge
when an agent is in the manifest but failed to connect at startup. Two
processes are involved:

- **`scripts/arc-stack.sh`** — the systemd ExecStart wrapper. Iterates
  agents, attempts each one, knows the success/failure outcome.
- **`packages/arcui/src/arcui/routes/team_pages.py`** — the route serving
  `/api/team/roster`. Needs to know the per-agent state at request time.

These run as the same user but in different processes, with no shared
in-memory state. Architecture review (Minor #3) flagged that the chosen
file-based handoff is **not documented** in the SDD §"Architecture Overview".

## Decision

**`~/.arcagent/agent-state.json` is the contract.**

### Schema

```json
{
  "_meta": {"written_at": "2026-05-06T13:42:01Z"},
  "<agent_id>": "connected" | "degraded",
  ...
}
```

### Writer (arc-stack.sh)

- Writes the file atomically: `( umask 077; printf > tmp )` then `mv tmp final`.
- The `umask 077` lives **inside** the parens so the tmp file is born 0600;
  this closes SPEC-025 §L1 (the previous `( umask 177; mv ... )` only chmod'd
  the rename, not the create).
- `_meta.written_at` is an ISO-8601 UTC timestamp set immediately before write.

### Reader (team_pages.py `_load_agent_state`)

- Reads with `path.read_text(encoding="utf-8")` then `json.loads`.
- **Freshness check:** if `_meta.written_at` parses and is older than
  `_AGENT_STATE_MAX_AGE_SECONDS` (default 600s), the entire state is treated
  as missing — falls through to `degraded=False` for everything. Closes
  SPEC-025 §TD-5 (stale-read race during `arc-stack restart`).
- **Roster cross-check:** entries whose key is not an `agent_id` in the live
  roster are silently dropped. Closes SPEC-025 §M2.
- **Backward compatibility:** files without `_meta` are accepted. Operators
  who haven't redeployed `arc-stack.sh` yet still get the feature.

## Why a file, not a typed gateway endpoint?

Rejected alternatives:

- **Gateway-side `/health/agents` HTTP endpoint** — would need a new route
  layer in `arcgateway`, plus the executor exposing per-agent health.
  arcgateway has no per-agent-process supervisor today; `arc-stack.sh` IS
  the supervisor. Adding a Python supervisor to surface what bash already
  knows is yak-shaving.
- **NATS/Redis pub-sub** — overkill for a single-VM deploy; would force
  a new dependency.
- **Shared SQLite** — needs lock discipline across processes; the file +
  atomic-rename pattern is well-understood and uses one syscall.

The file-based approach is the simplest thing that works: arc-stack already
needs to write logs and tokens; one more file under `~/.arcagent/` is
zero-marginal-cost.

## Consequences

**Positive:**

- Zero new dependencies, zero new ports.
- Atomic write means arcui never sees a partial file.
- Freshness check eliminates the restart-race ambiguity.
- Tier-agnostic — works in air-gapped DOE deployments and commercial cloud.

**Negative:**

- `~/.arcagent/agent-state.json` is now a documented operator surface;
  changing the schema requires a deprecation cycle.
- Single-VM only — multi-instance arc-stack would need a different
  mechanism (NATS, gateway endpoint, or central state store). Out of scope
  for v1.1.

## Verification

- `tests/test_team_aggregations.py::TestRosterDegradedState` (4 base tests +
  2 new for stale + legacy)
- `tests/test_team_aggregations.py::TestRosterDegradedState::test_unknown_agent_id_in_state_file_is_dropped`
- `tests/test_team_aggregations.py::TestRosterDegradedState::test_stale_state_file_is_ignored`
- `tests/test_team_aggregations.py::TestRosterDegradedState::test_legacy_state_without_meta_is_accepted`

## Code anchors

- Writer: `scripts/arc-stack.sh:296-315` (the per-agent state-write block)
- Reader: `packages/arcui/src/arcui/routes/team_pages.py:42-104`
  (`_AGENT_STATE_FILE`, `_AGENT_STATE_MAX_AGE_SECONDS`, `_load_agent_state`,
  `_state_is_fresh`, `get_roster`)
