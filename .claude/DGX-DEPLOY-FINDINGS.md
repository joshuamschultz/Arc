# DGX deploy findings — 2026-08-13

> **Redeploy 2026-08-14:** items 1 and 2 are FIXED in the repo and the DGX now
> runs the SHIPPED `deploy/systemd/arc.service` verbatim (`diff` vs repo: 0
> lines) — no hand patches remain. Verified after: service active, health 200, 6
> agents, 18 modules per agent, 3 workflows visible, 8/8 audit chains verify, 0
> absent-module warnings, 0 tracebacks. `arc install` ran as `ExecStartPre` and
> was correctly idempotent ("nothing to build — every enabled module is already
> installed"). The home migration re-ran as a no-op (`already_migrated: True`).
> Items 3-6 remain open.

Problems found deploying `73eda1d9` (Arc-home split) to spark-0290. Fix these
locally; the DGX was patched by hand where noted, and a hand patch does not reach
the repo.

## 1. `arc_team()` points at the HIDDEN home — contradicts the agreed layout

`arctrust.paths.arc_team()` resolves to `~/.arc/team`. The agreed layout is agent
data in the **non-hidden** `~/arc/team/` — "all files from running agent, trace,
memory, schedule, etc are in non hidden arc folder (arc/team/...)".

The shipped `deploy/systemd/arc.service` inherits this and passes
`--team-root %h/.arc/team`, which on a real box is EMPTY — the 6 agents live at
`~/arc/team`. Starting from the shipped unit would load zero agents.

*DGX patched by hand:* unit points at `%h/arc/team`.
**Fix:** decide the canonical location, make `arc_team()` and the shipped unit
agree, and migrate if it is to move.

## 2. `workflows` is missing from the home migration — data goes invisible

`arctrust.home_migration._LAYOUT` carries `operator`, `identity`, `trust`,
`store`, `nats`, `bundles`, `extensions`, `capabilities`, `blueprints`, `modules`
— but **not `workflows`**. After migrating, the code reads
`<home>/state/workflows` while the existing bundles sit at `<home>/workflows`.

On the DGX that hid 3 live workflows: `client-update-workflow`,
`client-update-workflow-v3`, `seo`. Silent — nothing errors, the workflows simply
stop existing as far as the runner is concerned.

*DGX patched by hand:* `mv ~/.arc/workflows ~/.arc/state/workflows`.
**Fix:** add `"workflows": "workflows_dir"` to `_LAYOUT`, and add a test that
every accessor in `paths.py` naming a state subdirectory has a `_LAYOUT` entry —
this is the second instance (extensions was the first) and a list that must be
kept in sync by hand will drift again.

## 3. `evaluations/` — 12 failures, and it pollutes other suites

- Its scratch agent needs modules installed at the deployment module root and
  nothing in that suite installs them, so preflight fails
  `memory_module_configured`. SPEC-066 behavior, not a regression, but it means
  the eval suite has been broken since modules left the wheel.
- It has no `ARC_CONFIG_DIR` isolation fixture, so running it in the same session
  makes 3 arccli tests fail that pass in isolation
  (`test_cli_init_team.py` ×2, `test_identity_cmd.py::test_init_honors_dir_flag`).

**Fix:** give `evaluations/` the same autouse isolation fixture arcagent/arccli
now have, and have its harness install the modules its agent enables.

## 4. Cannot drive a chat turn over `/ws/chat` from a script

Repeated attempts to dispatch a turn over the WebSocket connect and authenticate
successfully, then close with no run dispatched and nothing in the journal. The
frame shape was taken from the real client
(`packages/arcui/web/src/hooks/use-chat.ts:231` — `{type:'message', text,
client_seq}`) and still did not dispatch.

Either the protocol needs another step the client performs elsewhere, or there is
a real defect in the non-browser path. **Unresolved — the fleet's ability to
complete a turn is therefore UNVERIFIED by me.** Worth an end-to-end test that
drives `/ws/chat` headlessly, since today nothing does.

## 5. Deep Olivia's model bridge pointed at a dead host

`~/.arc/bin/arc-litellm-forward.py` had `TARGET = "promaxgb10-4b18:4000"`, a host
no longer in the tailnet. Its systemd unit reported `active` because the listener
binds fine — it only fails on CONNECT, so it looked healthy while every
`simple_olivia` turn died with `RemoteProtocolError`.

*DGX patched by hand:* `TARGET = "inference-1:4000"`; verified `deepseek-v4`
returns real completions through the loopback.
**Fix:** the forwarder should fail its healthcheck when it cannot reach its
target, rather than reporting active. A bridge that binds but cannot forward is
the same "looks healthy, does nothing" failure mode as a degraded fleet.

## 6. Deployment hygiene

- Four stray 0-byte audit chains (`parity-agent`, `race-agent`,
  `delivery-agent`, `arcui`) were left in the real `~/.arc` by unisolated tests.
  Now under `state/store/worm/` after migration. Harmless, deletable.
- `sales_agent` enabled `[modules.memory_acl]`, whose source is an empty dir
  (only `__pycache__`). Removed from that agent's config by hand. The bundler
  correctly refuses it, but a stale config entry should be easier to spot —
  `arc up --check` does now report it as DEGRADED.

## What went right, and should not regress

`arc up --check` refused to start a fleet where all 6 agents were missing all 17
modules, and named every gap with its exact remedy. Without it the fleet would
have come up answering chat with no scheduler, no tasks, and no memory. The
`ExecStartPre=arc install` line in the shipped unit now makes that state
unreachable on restart.

All 7 operator-signed audit chains still verified after the home migration —
the acceptance bar for the whole reorg.
