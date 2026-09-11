# Supervised Bring-Up (`arc up`)

> **Runbooks**  ·  Operate  ·  page 2 of 20  
> **For** Operators deploying and running Arc  
> [← Overview](overview.md)  ·  [Docs home](../../README.md)  ·  [Local →](local.md)

```mermaid
flowchart LR
    classDef a fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef b fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef c fill:#002550,stroke:#001A38,color:#FFFFFF
    A["preflight<br/>nats · operator key · team · store"]:::a
    B["modules<br/>install what config enables"]:::a
    C["verify<br/>per-agent capability table"]:::b
    D["start<br/>arc ui start (embedded)"]:::c
    A --> B --> C --> D
```

`arc up` is one command that brings the whole stack up and **proves** it came up
whole: the agents, their modules, NATS, the gateway, and the dashboard.

## Why it exists

Modules do not ship inside the `arc-agent` wheel. Each one is a separately
signed bundle. So the ordinary update sequence —

```bash
git pull && uv sync --all-packages && systemctl --user restart arc.service
```

— leaves every agent with **zero** modules. And the agent still boots. It still
answers chat. The dashboard still looks green. Meanwhile its scheduler never
fires, its tasks never dispatch, and its memory never consolidates, because
`[modules.NAME] enabled = true` in its config names a folder that is not on
disk. Nothing crashed, so nothing was noticed.

`arc up` closes that gap in two places: it **installs** what each config asks
for, and it **refuses to start** a deployment that would still be missing
something afterwards.

## Usage

```bash
arc up                              # the whole thing: preflight → modules → verify → start
arc up --check                      # validate only; start nothing; exit non-zero on a problem
arc up --team-root ~/arc/team       # a team directory somewhere other than ./team
arc up --port 8420 --host 0.0.0.0   # passed straight through to `arc ui start`
arc up --no-install                 # inspect first; never writes a module
arc up --no-browser                 # headless box
```

| Flag | Meaning |
|------|---------|
| `--team-root <dir>` | Directory of `<name>/arcagent.toml` agents. Default `./team`. |
| `--port` / `--host` | Dashboard bind. Passed through unchanged. |
| `--gateway-config <file>` | `gateway.toml` for Slack/Telegram or a non-personal tier. |
| `--no-browser` | Do not auto-open a browser tab on a loopback start. |
| `--no-install` | Skip the module stage; report state without changing it. |
| `--check` | Preflight + verify only. Starts nothing. Non-zero on any problem. |

`--check` is what you run **before** a deploy and what CI runs against a node.
It never writes and never starts.

## The four stages

### 1. Preflight — check, never fix

| Check | Why it stops the bring-up |
|-------|---------------------------|
| `nats-server` on PATH | arcteam spawns the broker itself but cannot install it. Without the binary, team messaging and task dispatch are dead while the process stays healthy. |
| Operator key present | The trust anchor bundles, blueprints, and prompt overlays are pinned to, and the signer of the audit chain. **Never minted here** — an unpinned operator is no operator. Create one with `arc init`. |
| Team root | Must resolve and hold at least one agent. |
| Data dir | `arcstore`'s data directory must exist and be writable. |

Every failure stops the bring-up, because every one of them produces a
*silently* degraded agent rather than a crash.

### 2. Modules — install what the config enables

For each agent, `arc up` compares `[modules.NAME] enabled = true` against what
is actually materialized under `${ARC_CONFIG_DIR:-~/.arc}/runtime/current/modules/` and installs
the difference through the same verify → materialize → copy → enable path
[`arc module install`](../staging-module-bundles.md) runs. Nothing about
verification is bypassed.

The source is whatever the deployment permits — never a flag:

* a **staged bundle** in `${ARC_CONFIG_DIR:-~/.arc}/state/bundles/`, when one exists;
* otherwise the **development inner loop**, which is dev-signed and which
  `arcbundle` accepts at **personal tier only**.

On enterprise or federal the second path is refused, and the refusal names the
alternative:

```
Agent  Module   Action   Detail
josh   memory   REFUSED  no staged bundle for 'memory' in /home/arc/.arc/bundles, and a
                         federal deployment does not accept a development signature. Build
                         one on the low side with `arc module bundle memory` and stage it
                         here with `arc module install --from <bundle>`.
```

That is a *message*; the gate is `arcbundle`'s verifier, which refuses the
development issuer above personal tier however it was called.

Skip this stage with `--no-install` when you want to look before anything is
written.

### 3. Verify — before the start, not after

```
Agent  Module     State
-----  ---------  -------
josh   memory     present
josh   scheduler  present
josh   tasks      present
brad   memory     present
brad   tasks      MISSING

Service           State  Detail
----------------  -----  ---------------------------------------------
NATS              down   nothing listening at nats://127.0.0.1:4222
dashboard health  down   http://127.0.0.1:8420/api/health: Connection refused
  (services are reported as observed; before a start they are down by design)
```

Service rows are **observations, not verdicts**: before a start, NATS and the
dashboard are legitimately down — the bring-up is what starts them, and the
preflight already proved it can.

A `MISSING` module is the opposite. It is repeated on stderr with its remedy,
and it fails the command:

```
DEGRADED: brad enables module 'tasks' but it is not installed — the agent would
run without that capability and say nothing. Install it with:
arc module install tasks --agent brad

arc up: this deployment would come up degraded — nothing was started.
```

Exit code `1`, and **the server is never started**. Verify runs before the start
on purpose: refusing to start beats starting and complaining, because a started
process is what makes a degraded deployment look healthy.

A whole deployment looks like this and proceeds:

```
Agent  Module     State
-----  ---------  -------
josh   memory     present
josh   scheduler  present
josh   tasks      present
```

### 4. Start

`arc up` invokes `arc ui start` in-process — the same embedded-gateway path
[Local](local.md) documents: one process serving the dashboard, the web chat
WebSocket, the always-on fleet, and every enabled remote platform. It is not a
second launcher and it does not shell out.

`--port`, `--host`, `--team-root`, `--gateway-config`, `--no-browser`, and
`--verbose` pass straight through.

Once the server answers, the one check that cannot precede it is printed:

```
  Verified: http://127.0.0.1:8420/api/health responded 200
```

## Relationship to the other paths

| Path | Use it for |
|------|-----------|
| `arc install` | **Installing, without starting.** The same preflight + module stages, then it exits. What an upgrade and a systemd `ExecStartPre` run. |
| `arc up` | **Bringing a node up**, every time. Provisioning is already done. |
| [First-time provisioning](local.md) | **The one-time setup** — uv sync, installing `nats-server`, `arc init`, agent creation, `arc install`, the systemd unit. Run once. |
| `arc ui start` | The server itself. `arc up` calls it. Use directly when you have already verified the node. |
| `scripts/arc-stack.sh` | Local-dev only, explicitly non-canonical. Not a deployment path. |

## `arc install` — the same stages, without the start

`arc up` ends by handing the process to `arc ui start`, which blocks for the
lifetime of the server. When you want the *install* half and your shell back —
an upgrade, a provisioning script, a systemd `ExecStartPre` — that verb is
`arc install`:

```bash
arc install                          # every agent under ./team
arc install --team-root ~/arc/team   # a team directory somewhere else
```

Four stages: preflight → **bundles** → modules → verify. It re-reads every
config from disk at the end and exits `0` only when nothing an agent enables is
missing. After it, `arc up` finds nothing left to bootstrap.

### What it handles so you do not have to

| | |
|---|---|
| **Staging bundles** | A fresh box has an empty bundle store, so `arc module install --all` has nothing to install and does not say why. `arc install` builds and **operator-signs** a bundle for every module the fleet is missing, so `arc module bundle` is never a step you have to discover. Operator-signed rather than dev-signed means the result verifies at every tier and is built once for the whole fleet. |
| **The whole fleet** | Every agent under the team root, in one pass. Not `--agent <name>` in a loop. |
| **Partial failure** | An already-staged bundle is skipped, not refused; a module whose source cannot be packaged is reported and the rest continue. Re-running after something broke is the ordinary case. |
| **Late-joining agents** | Materialization is fleet-wide but the capability copy and the pinned issuer key are per agent. An agent added to an already-installed fleet gets its own copies — see below. |
| **Finding its own tools** | `nats-server` is resolved through `arcteam.find_nats_server`, which searches `~/.local/bin` and the other usual directories when `$PATH` does not carry it. |

### Presence is a per-agent question

A module is present *for an agent* only when both halves are true: the module is
materialized at the shared deployment root, **and** its capabilities are copied
into that agent's own tree with the issuer key pinned. Asking only the first
question let a three-agent fleet verify green while two of its agents had no
module tools at all — they had joined after the modules were materialized, so
every module looked present and the install skipped them.

### What it deliberately does not do

* **Sync the Python environment.** That environment provides the `arc` binary
  running the command; a process cannot rebuild the environment it is inside.
  `./install.sh` is the bootstrap that does, and it ends by calling this.
* **Mint an operator key.** The operator key decides what this deployment
  trusts; a bring-up that mints its own anchor has verified nothing. `arc init`
  mints it — and `./install.sh` runs `arc init` for you on a fresh box.

One asymmetry with `arc up`'s preflight is intentional: a missing `nats-server`
does **not** stop the install. The modules an agent enables have nothing to do
with the broker binary, and a box left with neither is strictly worse than a box
left with one. It is still a `FAIL` line and the command still exits non-zero.

**In a systemd unit**, keep `ExecStart` on `arc ui start` and put `arc install`
in `ExecStartPre` — `deploy/systemd/arc.service` ships it that way. A node whose
modules vanished under a `git pull && uv sync && systemctl restart` heals; a node
that genuinely cannot get them fails the unit instead of serving a hollow agent.
Use `arc up --check` there instead when you want the unit to fail rather than
write anything.

## Installing and upgrading a deployment

From a fresh clone, one command does the whole bootstrap — it has to be a shell
script because it creates the environment `arc` itself lives in:

```bash
git clone https://github.com/joshuamschultz/Arc.git && cd Arc
./install.sh analyst        # uv, deps, nats-server, config, agent, modules
arc up
```

Upgrading is the same command, because every step of it is idempotent:

```bash
git pull
./install.sh               # or just `arc install` on an already-synced box
systemctl --user restart arc.service
```

Skip it and the agents come back with zero modules, still answering chat.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `nats-server on PATH ... FAIL` | Broker binary absent. | Install from the [nats-server releases](https://github.com/nats-io/nats-server/releases) onto PATH (`scripts/install-nats.sh` sets up a local broker), or re-run your provisioning. |
| `operator key ... FAIL` | No key under `${ARC_CONFIG_DIR:-~/.arc}`. | `arc init`. `arc up` will not mint one for you. |
| `team root ... FAIL` | No `<name>/arcagent.toml` under the team root. | `arc agent create <name> --dir team`, or pass `--team-root`. |
| `REFUSED ... does not accept a development signature` | Enterprise/federal box, no staged bundle. | `arc module bundle <name>` on the low side; stage it and `arc module install --from <bundle>`. |
| `MISSING` after a successful install stage | The config enables a module name that does not exist in any catalog. | Fix the typo in `[modules.NAME]`, or `arc module list` to see the real names. |
| `HEALTH FAILED after 60s` | Server started but never answered `/api/health`. | Check the port is free and read the uvicorn output above the line. |

---

> [← Overview](overview.md)  ·  [Docs home](../../README.md)  ·  [Local →](local.md)
