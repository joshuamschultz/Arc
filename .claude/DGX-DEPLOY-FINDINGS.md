# DGX deploy findings

## Status — 2026-08-14

The fleet is live from the shipped `main` with **no hand patches in the code**:
service active, health 200, six agents, 18 modules each, three workflows, 8/8
audit chains verifying — and, for the first time, **every one of the six agents
verified completing a real chat turn** (`ARC-LIVE-OK`, including `simple_olivia`
on DeepSeek).

**All findings are closed.** Each was fixed in the repo with a test that
fails without the fix — none was patched on the box.

---

## The deploy that looked healthy and answered nothing

Two deploys in a row were declared healthy on: service active, `/health` 200,
zero absent-module warnings, zero tracebacks, and a green `arc up --check`. Every
message still answered `[agent-error] the run failed`.

Cause: each agent config carried `operator_key_dir = "~/.arc/operator"` — the
pre-split path, written by an old scaffold. The home migration moved the key to
`state/operator/`, so all six agents named an empty directory and
`ArcAgent.startup` fail-closed on the missing audit authority. The guard behaved
exactly right; nothing was checking it before the first message.

**Everything green was measuring something other than what a user does.** Health
checks the process. Preflight checked the deployment default key. Module warnings
check the filesystem. Not one of them ran a turn.

### Fixed

- **`arc up --check` now verifies the key each agent actually loads**, resolved
  through `arcagent.operator_key_path` — the same function `ArcAgent.startup`
  calls — and names the offending agents plus the remedy. Validated on the live
  box: it FAILED with all six agents named, and passed only after the configs
  were corrected.
- **`tests/journeys/`** — 19 tests that drive what people actually do against a
  real deployment with only the LLM wire scripted. Mutation-checked: resolving
  the operator key at its pre-split path fails three of them.
- **Live data repair**: `operator_key_dir` cleared in all six agent configs so
  the one resolver owns the path. Backups in `~/arc-toml-backup/`.

**A config that spells out a default is a landmine.** It cannot follow the
resolver when the layout changes, and nothing reports the drift. The current
scaffold writes `operator_key_dir = ""`; these configs predated it.

---

## Closed

**1. `arc_team()` pointed at the hidden home.** Fixed; the shipped systemd unit
now installs verbatim (`diff` vs repo: 0 lines).

**2. `workflows` missing from the home migration.** Fixed, plus a test that
derives the accessor list from `paths.py` so the two lists cannot drift again.
Three live workflows were invisible.

**4. Could not drive a chat turn from a script.** The protocol needs an auth
frame (`{"token": …}`) and its `ready` reply *before* the message frame; without
it the socket closes silently. `receive_json` also blocks with no deadline, so a
dead pipe hung the run instead of reporting. Both are handled in
`tests/journeys/`, and `/tmp/turn.py` on the box drives a turn on demand.

**7. `arc_team()` ignored `ARC_CONFIG_DIR`.** Introduced while fixing item 1 and
caught by the journey work: an isolated test resolved the developer's *real*
`~/arc/team` and could have created agents beside running ones.

---

## Also closed

**3. `evaluations/` — 13 failures, and it polluted other suites.** Both halves
fixed. The scratch agent enabled `[modules.memory]` but nothing installed it, so
on the throwaway home the harness builds, memory could not load and a *memory*
benchmark was measuring the absence of the thing under test; module
materialization now happens in `write_eval_agent_config`, through the same
installer `arc install` calls rather than a second path that could drift. The
pollution was already gone once arccli got its autouse `ARC_CONFIG_DIR` fixture.
One test also asserted `MemoryIsolationError` for an *unconfigured* module — it
predated the split that made "nothing installed" a separate, benign cause, and
now pins both: unconfigured is a plain refusal, unbound is the isolation fault,
and neither is ever a silent empty recall. 542 pass, 0 fail.

**5. The bridge that reported healthy while unable to forward.** It existed only
on the DGX, which is why it was never fixed: a load-bearing production component
living on one box is unreviewable and unshippable. It is now
`deploy/connect-forward.py` plus its unit, and it cannot repeat the failure —
the target is **required and never defaulted** (a built-in hostname that outlived
the machine it named is the whole bug), it is probed **before** binding so a
broken bridge never accepts a connection, and it is re-probed periodically and
exits when the target vanishes, so systemd's state tracks the path rather than
the socket. Five tests drive a real loopback CONNECT proxy; removing the startup
probe fails two of them, and removing the watcher's shutdown fails a third.

**6. Hygiene.** `witness_medium_path` defaulted to the literal
`~/.arc/witness/anchor.log` **in code** — the same landmine as `operator_key_dir`
and worse, since it was the shipped default, not stale scaffold data. It ignored
`ARC_CONFIG_DIR`, so every isolated deployment and every test wrote its federal
rollback anchor into the invoking user's real home, and the migration had no
entry to move it. Now `""` resolving `arctrust.paths.default_witness_medium_path()`,
with a `witness` migration entry — which the accessor/`_LAYOUT` drift test would
have demanded anyway. The stray 0-byte audit chains were left by the unisolated
tests that are now fixed; they are inert and deletable.

## New, and not mine to fix silently

**`sales_agent`'s `crm_pipeline` cannot run: the box user is not in the `docker`
group.** Isolated capabilities execute in a container, and `/var/run/docker.sock`
is `root:docker 0660`, so every invocation dies with
`permission denied ... unix:///var/run/docker.sock`. Pre-existing and unrelated to
this work; chat, schedules, tasks and memory are unaffected. The error message is
already precise, so this is a one-line box change — `sudo usermod -aG docker
joshuamschultz` and re-login — which needs root and is therefore Josh's call, not
something to do unannounced.

## What to keep

`arc up --check` refused to start a fleet missing all 17 modules and named every
gap with its remedy, and it now refuses one whose agents cannot sign. That is the
right shape: **preflight must check the thing the agent will actually load, not
the thing the deployment happens to have.** The distinction is the whole bug.
