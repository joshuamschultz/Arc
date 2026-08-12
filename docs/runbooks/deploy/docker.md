# Docker Deployment

> **Runbooks**  ·  Operate  ·  page 3 of 18  
> **For** Operators deploying and running Arc  
> [← Local](local.md)  ·  [Docs home](../../README.md)  ·  [Azure →](azure.md)

```mermaid
flowchart LR
    classDef a fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef b fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef c fill:#002550,stroke:#001A38,color:#FFFFFF
    A["build image"]:::a
    B["mount workspace<br/>agent state stays on the host"]:::a
    C["run container"]:::b
    D["Caddy ingress"]:::b
    E["health check"]:::c
    A --> B --> C --> D --> E
```

One image, three deployments: your laptop, a single node (DGX), and a public
VM with TLS. The container is the install path — there is no uv, no
`nats-server` download, no `arc init`, and no model pull to get right, because
all of it is baked into the image or done by the entrypoint on first boot.

Validated on: Docker 29.4, linux/arm64, personal tier, live Anthropic round
trip (`arc agent run` → `ready`), web dashboard, embedded gateway, auto-spawned
NATS JetStream broker.

## Quick start (local)

```bash
cp .env.example .env      # add ANTHROPIC_API_KEY
docker compose up -d
docker compose logs -f arc
```

The startup log prints the dashboard URL with the viewer token appended as a
fragment:

```
Dashboard: http://<host>:8420/#auth=<VIEWER_TOKEN>
```

The frontend reads `window.location.hash`, stores the token to `localStorage`,
and strips it from the address bar. Opening the bare URL works too — it falls
back to a paste-token login.

Retrieve the tokens again at any time:

```bash
docker compose exec arc cat /data/.arc/arc.env
```

## What the image contains

| Component | Why it is baked in |
|---|---|
| The uv workspace + venv | `uv sync --frozen` at build time — the lockfile is the contract |
| `nats-server` | Not a Python dep. `arcteam` auto-spawns `nats-server -js` as a child of `arc ui start`, but only if it is on `PATH`. Verified against the release SHA256SUMS |
| `sentence-transformers` + `all-MiniLM-L6-v2` weights | arcmemory's default embed backend is `local` on MiniLM. Without it, semantic recall and consolidation dedup degrade to a **silent** no-op. Weights are baked so first boot needs no HuggingFace call and works air-gapped (`HF_HUB_OFFLINE=1`) |
| The arcui static bundle | Committed to the repo, so the image needs no Node toolchain |

Torch is installed from PyTorch's CPU index on **both** architectures — the
default PyPI wheel drags ~3 GB of `nvidia-*` CUDA packages that never execute
on a CPU-only VM, on aarch64 as well as x86_64.

## State and the /data volume

`HOME=/data`, which is what puts every default on the volume at once:

| Path | Contents |
|---|---|
| `/data/.arc/` | `arcllm.toml`, `arcagent.toml`, `gateway.toml`, `arc.env` (0600) |
| `/data/.arc/store/` | `arcui.db` — sessions, traces, tasks |
| `/data/.arcagent/keys/` | Agent identity keys |
| `/data/team/<agent>/` | Agent config, workspace, memory, capabilities |

`docker compose down` keeps the volume. **`docker compose down -v` destroys the
agent's identity and memory permanently.**

The entrypoint is idempotent: on restart it detects existing config and leaves
it alone. Viewer/operator tokens are minted exactly once and pinned for the
life of the volume — the UI derives its chat-session id from the viewer token,
so regenerating on restart would strand every prior conversation.

## Configuration

Everything is env vars (see `.env.example`). The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. The entrypoint fails closed without it |
| `ARC_AGENTS` | `arc_agent` | Space-separated for a fleet; the **first** receives remote-platform DMs |
| `ARC_AGENT_MODEL` | `anthropic/claude-sonnet-5` | |
| `ARC_TIER` | `personal` | |
| `ARC_ENABLE_TELEGRAM` | `0` | Requires `ARCAGENT_TELEGRAM_BOT_TOKEN` |
| `ARC_TELEGRAM_ALLOWED_USER_IDS` | empty | **Empty means deny everyone** — the fail-closed authorization gate |

## Online deployment (public VM, TLS)

`deploy/cloud/` adds Caddy in front of Arc for automatic Let's Encrypt
certificates.

```bash
cd deploy/cloud
cp ../../.env.example .env      # set ANTHROPIC_API_KEY, ARC_DOMAIN, ARC_ACME_EMAIL
docker compose up -d
```

`ARC_DOMAIN` must already resolve to the VM's public IP before you start the
stack — Caddy's ACME HTTP-01 challenge is served on `:80` and fails otherwise.

Two deliberate differences from the local compose:

- **Arc publishes no host port.** It is reachable only on the compose network,
 so Caddy is the single ingress and the dashboard cannot be served over
 plaintext. Verified: `:8420` on the host is refused.
- **Caddy's `/data` and `/config` are named volumes.** Certificates must
 survive `down`/`up`, or a redeploy re-requests them and burns Let's Encrypt
 rate limit.

Auth is enforced through the proxy exactly as it is directly: `/api/health` is
auth-exempt (liveness probes need no credentials), everything else returns 401
without a token.

### Unattended provisioning

`deploy/cloud/cloud-init.yaml` provisions a fresh VM end to end: installs
Docker, writes the compose file, Caddyfile, and a 0600 `.env`, then starts the
stack. A provisioner substitutes the `__PLACEHOLDER__` tokens before submitting
it as user-data.

Secrets are written by cloud-init straight to a 0600 file rather than echoed
through a shell command, so they stay out of the process table and shell
history.

Readiness is the public `https://$ARC_DOMAIN/api/health` returning
`{"status":"ok"}`.

## Building

```bash
docker build -t arc:latest .
```

For a fleet spanning architectures (DGX Spark is aarch64, most cloud VMs are
x86_64):

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/arc:latest --push .
```

Deploying by pulling a prebuilt image is strongly preferred over building on
the target — a 2-vCPU cloud VM takes far longer to build the image than to pull
it, and build time lands inside the provisioning budget.

## Troubleshooting

**`arc agent run` inside a running container fails with `WormSink: another
writer holds...`** — expected, not a bug. The audit chain is single-writer and
the service already holds it. To run a one-off turn, stop the service and use
the same volume:

```bash
docker compose stop arc
docker run --rm -v arc_arc-data:/data --env-file .env --entrypoint sh arc:latest \
  -c '. /data/.arc/arc.env && arc agent run /data/team/arc_agent "hello"'
docker compose start arc
```

**Startup warns `sentence-transformers MISSING` or `sqlite-vec NOT loadable`** —
the entrypoint preflights both and says so loudly, because both failure modes
otherwise look identical to "working" from the outside: recall silently falls
back to keyword-only. In a stock image neither should ever appear.

**`Config enables module 'memory_acl' but no module folder is present`** —
harmless; the module is configured but not shipped, and startup continues.

**Telegram adapter warnings on a network blip** — see
[single-node.md](local.md); the same behavior applies in the container.
