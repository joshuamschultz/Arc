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
> at every tier. `scripts/arc-stack.sh` — the older multi-process pattern
> (`arc agent serve` per agent + `arc ui start` as a pure `arcstore`
> reader, registered under `$TEAM_ROOT/shared`) — is non-canonical, dev
> tooling at most. The standalone `arcgateway start` daemon should
> eventually fail closed and point operators at the embedded path instead
> of echo-stubbing (see Troubleshooting). Don't mix `arc-stack.sh`'s
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
workspace member. As of this writing that means `arcgateway-telegram`
(and `-slack`, `-mattermost`) are **not** installed by default: they're
workspace members but aren't declared in root `pyproject.toml`'s
`[project.dependencies]`. Until that lands, install whichever platform
adapter you need directly into the venv:

```bash
ssh host 'cd ~/arc && .venv/bin/python -m pip install -e packages/arcgateway-telegram --no-deps'
```

Verify: `.venv/bin/python -c "import arcgateway_telegram"` should succeed
with no traceback. `deploy-node.sh` checks whether the root-dependency fix
has shipped (greps `pyproject.toml` for the package name) and skips this
step automatically once it has — no flag to flip, it just stops being
necessary.

**Gotcha**: `uv sync` **uninstalls** a manually-`pip install -e`'d package
that isn't part of its resolved dependency set, every time it runs. If you
(or CI, or a future script) run a bare `uv sync` after this workaround is
in place, Telegram support silently disappears until someone reinstalls
it. `deploy-node.sh` re-checks after every `uv sync` for exactly this
reason — don't skip that ordering if you're scripting this by hand.

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

Apply these deltas on top of the generated defaults — **verify field names
against the Pydantic models, not the generated comments**; the generator
has at least one known bug (see Troubleshooting). `scripts/
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
token_env = "TELEGRAM_BOT_TOKEN"     # NOT bot_token_env — see Troubleshooting
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

## Troubleshooting

**"adapter 'telegram' enabled but its plugin package is not installed"**
(warning in `journalctl --user -u arc.service`) — the `arcgateway-telegram`
package isn't in the venv. See Install § above; this is the `uv sync`
uninstall gotcha, not a one-time fluke. Restart the service after
installing the package.

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
