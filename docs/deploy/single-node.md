# Single-Node Deployment (embedded gateway)

This is the validated runbook for standing up Arc on a single box — one
`systemd --user` unit running `arc ui start` with an **embedded** gateway
(SPEC-023). One process serves the dashboard, the web chat WebSocket, and
every enabled remote platform (Telegram, Slack, …) in-process. There is no
standalone `arcgateway start` daemon and no separate `arc agent serve`
process per agent — the embedded gateway's agent factory loads each agent
from `--team-root` on demand.

`scripts/deploy-node.sh` automates every step below and is safe to re-run
(idempotent). This doc explains what it does and why, for anyone deploying
by hand or debugging a failed run.

> **Architecture ruling (2026-07-10): the embedded pattern is canonical**
> at every tier — and, as of the latest fix, the *only* working
> agent-execution path at every tier. `scripts/arc-stack.sh` — the older
> multi-process pattern (`arc agent serve` per agent + `arc ui start` as a
> pure `arcstore` reader, registered under `$TEAM_ROOT/shared`) — is
> non-canonical, dev tooling at most. The standalone `arcgateway start`
> daemon now unconditionally refuses to start at **every** tier, including
> federal (see Troubleshooting) — personal/enterprise has no agent_factory
> standalone, and federal's `SubprocessExecutor` worker is DID-blind
> (ignores the requested `agent_did`, always loads a fixed-path config), so
> neither can correctly serve a real gateway. Don't mix `arc-stack.sh`'s
> `$TEAM_ROOT/shared` registration root with this doc's `$TEAM_ROOT`
> convention if you still have a reason to run it.

Validated on: NVIDIA DGX Spark, aarch64, Ubuntu 24.04, Python 3.12, personal
tier, four-agent fleet, live Telegram round-trip (session written, memory
captured, traces stored).

## Prerequisites

- SSH access to the target host, a non-root user, `curl`. No `sudo`
  required for anything in this doc.
- **uv** — the Python package/venv manager Arc's workspace uses.
- **nats-server binary** — `arcteam` needs a JetStream-enabled NATS broker
  for the entity registry, signed audit chain, and messaging streams. It
  is **not** a Python dependency — `arcteam.nats_server.ensure_nats_server()`
  auto-spawns `nats-server -js` as a supervised child of `arc ui start` if
  one isn't already reachable, but only if the binary is on `PATH`. Install
  the official release binary for the host architecture to `~/.local/bin`;
  `scripts/deploy-node.sh` resolves the latest release via the GitHub API
  and verifies its `SHA256SUMS` before installing.
- **Secrets** — `ANTHROPIC_API_KEY` (required) and, if enabling Telegram,
  a bot token from [@BotFather](https://t.me/BotFather). Keep them in a
  `.env` file the deploy script reads from — never commit it, never print
  it, never let it appear in an agent's transcript.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv --version
```

The installer wires `~/.local/bin` into `~/.profile`/`~/.bashrc` for
**login** shells. Non-interactive `ssh host 'cmd'` sessions don't source
those files, so scripted commands should either use the absolute path or
export `PATH` explicitly — `deploy-node.sh` does this for you.

## Install

Sync the repo (rsync from a dev machine, or `git clone` directly on the
target — either works; `deploy-node.sh` assumes it's already run from the
repo root):

```bash
rsync -az --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
  --exclude .ruff_cache --exclude .mypy_cache --exclude .env \
  --exclude .arc-logs --exclude dist \
  /local/path/to/arc/ host:~/arc/
```

```bash
ssh host 'cd ~/arc && ~/.local/bin/uv sync'
```

`uv sync` installs the root project's dependency closure — **not** every
workspace member. `arcgateway-telegram` **is** declared in root
`pyproject.toml`'s `[project.dependencies]` (fixed 2026-07-10, alongside
`arcmemory`/`arcskill` for the same reason: the default extension must
load out of the box), so a bare `uv sync` installs it — no extra step
needed. `-slack`/`-mattermost` aren't pinned yet; install those the same
way if you need them:

```bash
ssh host 'cd ~/arc && .venv/bin/python -m pip install -e packages/arcgateway-slack --no-deps'
```

Verify Telegram: `.venv/bin/python -c "import arcgateway_telegram"` should
succeed with no traceback straight after `uv sync`, with no manual install
step. `deploy-node.sh` still carries a defensive check (greps
`pyproject.toml`, falls back to a manual install only if the dependency
somehow isn't there) — a no-op today, kept as a safety net rather than
deleted.

If an enabled adapter's package is missing at runtime, the gateway doesn't
crash — it logs a warning and skips that platform (see Troubleshooting).

## Configure

### Secrets file

Create `~/.arc/arc.env` (0600) with the values `arc ui start` needs at
runtime. Do this in a single remote shell invocation so secret values never
appear in your local terminal history or an orchestrating agent's
transcript:

```bash
ssh host 'bash -s' <<'REMOTE'
set -euo pipefail
mkdir -p ~/.arc
ANTHROPIC_API_KEY=$(grep -m1 '^ANTHROPIC_API_KEY=' ~/arc/.env | cut -d= -f2-)
TELEGRAM_BOT_TOKEN=$(grep -m1 '^ARCAGENT_TELEGRAM_BOT_TOKEN=' ~/arc/.env | cut -d= -f2-)
VIEWER_TOKEN=$(openssl rand -hex 32)
OPERATOR_TOKEN=$(openssl rand -hex 32)
umask 077
cat > ~/.arc/arc.env <<INNER
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
VIEWER_TOKEN=$VIEWER_TOKEN
OPERATOR_TOKEN=$OPERATOR_TOKEN
INNER
chmod 600 ~/.arc/arc.env
REMOTE
```

Generate `VIEWER_TOKEN`/`OPERATOR_TOKEN` **once** and persist them —
regenerating on every restart breaks the (agent, user) → chat-session-id
mapping the UI derives from the viewer token, stranding prior session
history. `scripts/arc-stack.sh` encodes the same lesson via its own pinned
token file; `deploy-node.sh` leaves `arc.env` untouched if it already
exists for the same reason.

### `arc init` and the three config files

```bash
ssh host 'cd ~/arc && .venv/bin/arc init --tier personal --provider anthropic'
```

Writes `~/.arc/{arcllm.toml,arcagent.toml,gateway.toml}`. `deploy-node.sh`
skips this step if `gateway.toml` already exists — re-running `arc init`
against an already-customized host would either hang on an interactive
overwrite prompt or (with `--quick`) silently clobber those customizations.

`arc init`'s generator now writes correct defaults out of the box (fixed
2026-07-10): the `token_env`/`bot_token_env` mixup is gone, and
`[modules.skills]`/`[modules.skills.config]` (nested exactly as shown
below) now ships in the baseline `arcagent.toml` — reapplying it is
harmless but no longer necessary. `[eval].provider`/`.model` are still
generated empty (`""`) and need setting by hand; that's the one delta
below that isn't yet part of `arc init`'s output. `scripts/
deploy_node_overlays.py` applies all of these idempotently via `tomlkit`
(preserves comments/formatting, safe to re-run, never clobbers a value you
set by hand unless you re-pass the matching flag):

**`~/.arc/arcagent.toml`** — model for policy eval and the skill improver,
plus the skills adapter:

```toml
[eval]
provider = "anthropic"
model = "claude-sonnet-5"

[modules.skills]
enabled = true

[modules.skills.config]
adapter = "arcskill"
tier = "personal"
```

Note the **nested** shape — `enabled` lives at the module level, while
`adapter`/`tier` live one level deeper under `[modules.skills.config]`
(`arcagent/modules/skills/config.py::SkillsConfig`). A flat
`[modules.skills] adapter = "arcskill"` looks plausible but is wrong.

**`~/.arc/gateway.toml`** — web chat adapter (required when passing an
explicit `--gateway-config`; omitting the flag auto-builds a web-only
default, but an explicit file must opt in itself) and Telegram:

```toml
[gateway]
tier = "personal"
agent_did = ""   # filled in after agent create, below

[platforms.web]
enabled = true

[platforms.telegram]
enabled = true
token_env = "TELEGRAM_BOT_TOKEN"     # NOT bot_token_env (that's Slack's field name)
allowed_user_ids = []                 # empty = deny all (fail-closed)
```

`allowed_user_ids` is the working authorization gate today — DM pairing
landed (task #7) but `allowed_user_ids` remains the primary personal-tier
control; put the Telegram user id(s) that should reach the agent here.
Get a user's id by having them message the bot once and checking the
adapter's audit log for the rejected `user_id` (or `@userinfobot`).

## Agent create

```bash
ssh host 'cd ~/arc && export PATH="$HOME/.local/bin:$PATH" && \
  set -a && source ~/.arc/arc.env && set +a && \
  .venv/bin/arc agent create josh_agent --dir team --model anthropic/claude-sonnet-5'
```

Auto-registers with arcteam if the NATS broker is reachable — it will be,
once `arc ui start` has run once and spawned its managed broker (see
below), or if you start `nats-server -js` yourself first. Prints the
minted DID; copy it into `gateway.toml`'s `[gateway].agent_did` so the
embedded gateway knows which identity to route platform DMs to
(`deploy_node_overlays.py gateway-config --agent-did <DID>`).

Apply the same `[eval]`/`[modules.skills]` deltas to
`team/<agent>/arcagent.toml` too, even though the user-wide
`~/.arc/arcagent.toml` already sets them — belt-and-suspenders against the
per-instance merge missing them.

Validate before wiring into systemd:

```bash
.venv/bin/arc agent build team/josh_agent --check
```

Expect: `model: anthropic/claude-sonnet-5`, `ANTHROPIC_API_KEY is set`,
tool/strategy listing, `Ready.` A TOFU capability-load warning for the
scaffolded `calculator.py` is expected right now — see Troubleshooting.

For multiple agents (a fleet) and the team/channel/persona flow, see
[team-building.md](./team-building.md).

## systemd user unit

`~/.config/systemd/user/arc.service` (the exact file — `deploy/systemd/
arc.service` in this repo, installed verbatim by `deploy-node.sh`):

```ini
[Unit]
Description=Arc — UI + embedded gateway (web chat, Telegram)
After=network-online.target

[Service]
WorkingDirectory=%h/arc
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EnvironmentFile=%h/.arc/arc.env
ExecStart=%h/arc/.venv/bin/arc ui start --host 0.0.0.0 --port 8420 --team-root %h/arc/team --gateway-config %h/.arc/gateway.toml --no-browser --viewer-token ${VIEWER_TOKEN} --operator-token ${OPERATOR_TOKEN}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

- `%h` is systemd's native home-directory specifier — confirmed working
  (`systemctl status` shows both `WorkingDirectory` and `ExecStart`
  resolved to the real home path).
- `Environment=PATH=...` must include `~/.local/bin` — that's how
  `shutil.which("nats-server")` finds the broker binary at startup, so
  `arc ui start` can auto-spawn its own managed NATS child bound to a
  persistent store dir (`~/.arc/nats/jetstream`, survives restarts).
- `EnvironmentFile=%h/.arc/arc.env` supplies `ANTHROPIC_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `VIEWER_TOKEN`, `OPERATOR_TOKEN` at process start.

**Known limitation — token exposure in `ps`**: `${VIEWER_TOKEN}`/
`${OPERATOR_TOKEN}` substitute directly into `ExecStart`'s argv (systemd
does support `${VAR}` expansion from `EnvironmentFile` in exec lines), so
both tokens are visible to any local user via `ps aux` / `systemctl
status`. `arc ui start` has no environment-variable fallback for these —
only CLI flags (`packages/arccli/commands/ui.py`) — so there's no way to
avoid this from docs/deploy/scripts alone; it needs a code change to
accept `VIEWER_TOKEN`/`OPERATOR_TOKEN` from the environment when the flags
are omitted. Acceptable on a single-user, network-gated (e.g. Tailscale)
host; not appropriate as-is for a shared multi-user host.

```bash
systemctl --user daemon-reload
systemctl --user enable --now arc.service
loginctl enable-linger "$USER"   # survive logout/reboot without sudo
```

## Verify

```bash
curl -s http://127.0.0.1:8420/api/health                       # {"status":"ok"}, no auth required
```

```bash
curl -s -H "Authorization: Bearer $VIEWER_TOKEN" \
  http://127.0.0.1:8420/api/team/roster                        # every agent, model, provider
```

```bash
.venv/bin/arc ext inspect --agent team/josh_agent               # confirms brain=arcmemory, skills=arcskill both "builtin"/"yes"
```

Smoke test an actual agent turn:

```bash
.venv/bin/arc agent run team/josh_agent "Reply with the single word: ready"
```

To confirm a remote-platform adapter (e.g. Telegram) is actually connected
— its own connect/loaded log lines are `.info()` calls and are suppressed
by the effective root log level (see Troubleshooting) — check for an
established outbound TLS connection from the `arc` process to the
platform's API range rather than relying on logs alone:

```bash
ss -tnp | grep "$(systemctl --user show -p MainPID --value arc.service)"
# Telegram Bot API lives in 149.154.160.0/20 — look for :443 to that range
```

## Access

Browser access uses a URL **fragment**, not a query param — the bundled
frontend regexes `window.location.hash` for `[#&]auth=([^&]+)`, stores the
token to `localStorage`, then strips it from the address bar:

```
http://<host>:8420/#auth=<VIEWER_TOKEN>
```

If you open the bare URL with no fragment, the dashboard falls back to a
manual "paste a viewer or operator token" login field — both paths work,
the fragment just skips the extra click.

Telegram: message the bot directly. Only user ids in
`[platforms.telegram].allowed_user_ids` get routed to the agent; everyone
else is rejected and audited (their `user_id` is logged, so you can add it
after the fact without guesswork).

## Dashboard capabilities (Reality Mirror)

Beyond chat, the dashboard is a real window into each agent's on-disk
state — reads it live, not a synced copy — and, for a few mutation paths,
an operator can act directly through the UI instead of SSHing in. Auth is
two roles, same tokens as above:

- **viewer** (`VIEWER_TOKEN`) — read-only across every view below: chat,
  browse memories/entities, read workspace files, list channels.
- **operator** (`OPERATOR_TOKEN`) — everything a viewer can do, plus every
  mutation: edit/delete a memory, save a workspace file, create a channel,
  add/remove channel members. Every operator mutation is audited through
  a single emission point (`emit_mutation_audit`) — actor role, session
  id, target, operation, and outcome, not a synthesized "success" flag.

| View | What it shows | Mutations (operator only) |
|---|---|---|
| **Knowledge** | An agent's episodic memories and entities — paged, ranked search, link navigation between a memory and the entities it tagged. Metadata columns: created, recency, importance (1–10), source. | Edit a memory's text, adjust importance/salience, delete an entry. |
| **File editor** | Rendered markdown (or raw text) for any file under the agent's workspace, opened from the file tree. | Save changes in place. If the file has a signature sidecar (`.arcsig`), the response after saving says **signature_stale** — the UI holds no agent identity and never signs on the agent's behalf; the agent re-signs it on next load. |
| **Channels** | Every `arcteam` channel and its members, sourced live from the messaging service (503 with a clear error if the service isn't wired, not a silent empty list). | Create a channel, add or remove members (resolves agent refs the same way the CLI does). |
| **Capabilities** | Every skill and tool an agent can load, across all four scan roots, with the loader's own verdict rendered verbatim (loaded / denied / unsigned / invalid) as a status badge — not a UI guess. Denial reasons are visible in a popover. | Read-only — capability trust decisions are made by the loader at agent startup, not from the dashboard. |

An empty Knowledge/Channels view (nothing captured yet, service not
configured) is shown distinctly from an *unreadable* one (a real error —
disk, permissions, an unwired service) — the dashboard never silently
collapses "nothing here" and "something's broken" into the same blank
state.

All of the above works per-agent from the same `arc ui start --team-root`
process described above — no extra flags, no separate service.

## Troubleshooting

**`arcgateway start` refuses immediately, prints a message about
`AsyncioExecutor`/`arc-agent-worker`, exits 1** — expected, not a bug in
your setup. The standalone daemon has no working agent-execution path at
*any* tier and unconditionally refuses rather than silently serve no real
agent (personal/enterprise) or the wrong one (federal — `SubprocessExecutor`'s
`arc-agent-worker` ignores the requested `agent_did`, always loading a
fixed-path config). Use the embedded path instead — it's the only one
that works today, at every tier:

```bash
arc ui start --team-root team --gateway-config ~/.arc/gateway.toml
```

`arcgateway stop`/`status` still work normally for managing a daemon
started before this fix landed. `[gateway].team_root` and
`arcgateway start --team-root` no longer exist (removed — they briefly
existed as a personal/enterprise-only fix before the federal case was
found and standalone was blocked everywhere); don't set them.

**"adapter 'telegram' enabled but its plugin package is not installed"**
(warning in `journalctl --user -u arc.service`) — historical: before
2026-07-10, `arcgateway-telegram` wasn't a root dependency and `uv sync`
would silently skip it (and actively uninstall a manually-added copy on
every re-run). Fixed — a bare `uv sync` now installs it. If you still see
this warning, something more unusual is wrong (e.g. a stale venv predating
the fix, or a workspace lock issue) — `uv sync` again and re-check
`.venv/bin/python -c "import arcgateway_telegram"` before assuming it's
this old gotcha resurfacing.

**`HTTP 400: temperature is deprecated for this model`** on any
`arc agent run` / smoke test — **fixed** as of this writing (`arcllm` no
longer sends an unconditional `temperature` to models that reject it), but
if you see this again: the symptom is the Anthropic API rejecting a
`temperature` value that arcllm's request builder sent by default from
Pydantic config (e.g. `[llm].temperature` in a scaffolded
`arcagent.toml`); the cause was arcllm not being model-aware about which
models accept the parameter. If it recurs on a newer model, that's the
same class of bug.

**"Capability load error .../calculator.py: tofu: deny"** — every
freshly-`arc agent create`d agent scaffolds a `capabilities/calculator.py`
that its own trust-on-first-use gate then denies at build/run time. Known
issue, tracked separately; non-fatal (execution continues past it), but
expect to see it on every agent until it's fixed.

**No "TelegramAdapter: connecting" / "registry: loaded adapter" in logs
even though it's working** — those are `.info()`-level log calls and the
effective root log level is `WARNING`, so they're silently dropped; only
the failure-path `.warning()` calls show up. Don't conclude the adapter
failed to connect from missing logs alone — use the `ss -tnp` check in
Verify § instead.

**Two bots, same box, wrong one answering (or neither)** — if you've
rotated `TELEGRAM_BOT_TOKEN` (e.g. switched to a different `@BotFather`
bot) and messages seem to go nowhere, confirm which bot a given token
actually belongs to before chasing a config bug:

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
```

Returns the bot's `username`/`id`. If it doesn't match the bot you're
DMing in Telegram, you're pointed at the wrong token — check
`~/.arc/arc.env` and restart the service after fixing it (the allowlist
and token are both read at adapter construction, not live-reloaded).

## Deployment pattern: embedded is canonical

The embedded pattern this doc describes is canonical at every tier
(architecture ruling, 2026-07-10). `scripts/arc-stack.sh` predates SPEC-023
and is being repositioned as dev tooling rather than a deployment path —
its different registration root (`$TEAM_ROOT/shared` vs. this doc's
`$TEAM_ROOT`) and process model (`arc agent serve` per agent) are not
interchangeable with anything here; don't copy assumptions across the two
until that repositioning lands.
