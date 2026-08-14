# DGX deploy findings

## Status — 2026-08-14

The fleet is live from the shipped `main` with **no hand patches in the code**:
service active, health 200, six agents, 18 modules each, three workflows, 8/8
audit chains verifying — and, for the first time, **every one of the six agents
verified completing a real chat turn** (`ARC-LIVE-OK`, including `simple_olivia`
on DeepSeek).

Items 1, 2, 4 and 7 are closed. Items 3, 5 and 6 remain open.

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

## Open

**3. `evaluations/` — 12 failures, and it pollutes other suites.** Its scratch
agent needs modules installed at the deployment module root and nothing in the
suite installs them; it also has no `ARC_CONFIG_DIR` isolation fixture, so it
makes three arccli tests fail that pass alone. Give it the autouse isolation
fixture arcagent/arccli now have, and have its harness install the modules its
agent enables.

**5. The model bridge reports healthy while unable to forward.**
`~/.arc/bin/arc-litellm-forward.py` binds fine and only fails on CONNECT, so
systemd calls it active while every `simple_olivia` turn dies. It should fail its
healthcheck when it cannot reach its target. (Target is currently correct, and
`simple_olivia` answers.)

**6. Deployment hygiene.** Four stray 0-byte audit chains (`parity-agent`,
`race-agent`, `delivery-agent`, `arcui`) left by unisolated tests — harmless,
deletable. `witness_medium_path` still names the pre-split `~/.arc/witness/…` in
every config; unused at personal tier, but the same latent defect as
`operator_key_dir` and it will bite at federal.

---

## What to keep

`arc up --check` refused to start a fleet missing all 17 modules and named every
gap with its remedy, and it now refuses one whose agents cannot sign. That is the
right shape: **preflight must check the thing the agent will actually load, not
the thing the deployment happens to have.** The distinction is the whole bug.
