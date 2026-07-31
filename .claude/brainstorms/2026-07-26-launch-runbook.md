# Arc SaaS Launch — Runbook & Session State

**Date:** 2026-07-26 · **Companion to:** [`2026-07-26-sales-exec-blueprint.md`](./2026-07-26-sales-exec-blueprint.md)

Operational state so this can be picked up cold. Design lives in the companion doc.

---

## Goal

> Email a friend a link → he pays $20/mo → he has an Arc agent in the cloud, connected to
> Telegram, working.

---

## Branch state

| Branch | Contains | Status |
|---|---|---|
| `main` | — | behind |
| `feat/quick-deploy` | Docker image, compose, cloud-init, Caddy TLS; `arc agent build/create --tier`; config-split + DID fixes; arctui + contributor docs | **pushed, deployed to DGX, not merged** |
| `feat/blueprints` | this doc + design doc | current |

**The DGX (`spark-0290`) is running `feat/quick-deploy`, not `main`.** Roll back with
`git checkout main && uv sync && systemctl --user restart arc.service`.

Merging `feat/quick-deploy` → `main` is an open decision.

---

## What already works (verified, don't rebuild)

- **One Docker image** — builds multi-stage, 2.2GB (was 5.5GB before collapsing venv layers),
  boots cold to healthy in ~6s. `docker compose up -d`.
- **Baked in:** uv workspace, `nats-server` (SHA256-verified), sentence-transformers +
  `all-MiniLM-L6-v2` weights (offline, `HF_HUB_OFFLINE=1`), arcui static bundle.
- **`HOME=/data`** puts config, store, and identity keys on one volume.
- **Idempotent entrypoint** — `arc init` → overlays → `arc agent create` → `--check` → serve.
  Tokens minted once and pinned. Preflights embedder + sqlite-vec **loudly**.
- **Online stack** — Caddy is sole ingress (Arc publishes no host port), auto-TLS, HSTS,
  auth enforced through the proxy (401), `:8420` refused on host. All verified locally.
- **`deploy/cloud/cloud-init.yaml`** provisions a fresh VM end-to-end.
- **Live LLM turn verified inside the container** (`arc agent run` → `ready`).

## Fixed along the way

- **Config-split stale reads** — `[llm]` moved to `arcllm.toml`, but `arc agent build --check`,
  `arc agent status`, and the arcui roster still read it from `arcagent.toml`. `--check`
  reported "No model configured" for healthy agents — and it gates every automated bootstrap.
  **Was live on the DGX** (all 4 agents showed `model=None`); fixed and verified there.
- **DID corruption in `deploy-node.sh`** — `sed` substitution left the trailing TOML comment
  attached to the DID. DGX's `gateway.toml` was *not* corrupted (verified), but a re-run would
  have. Now parsed with `tomllib`.
- **`arc agent build`** — was an interactive wizard that wrote `[llm]` to the wrong file,
  hardcoded personal tier, blanked the minted DID, and needed a TTY (unusable in automation).
  Now a deterministic tier-aware generator; `--force` required to overwrite; DID preserved.
- **Federal template didn't load** — `SecurityConfig` refuses an explicitly weaker crypto
  value fail-closed, so `--tier federal` emitted `require_fips=false` and `load_config` rejected
  it. Crypto knobs now render per tier from `SECURITY_CONFIG_KNOBS`.
- **arcui inventory seam** — `read_agent_tier` moved to `arctrust.policy`; the architecture
  test suite is green for the first time since the arcprompt merge.

---

## Step 1 — Accounts (only Josh can do these) — **BLOCKING**

| # | Account | What to get | Notes |
|---|---|---|---|
| 1 | **GitHub / ghcr.io** | Classic PAT, scope `write:packages` | Then set package **public**, or every VM needs a pull secret |
| 2 | **Hetzner Cloud** | Project + API token (Read/Write) | CX22 ≈ $4/mo |
| 3 | **Domain + Cloudflare** | Domain on Cloudflare; token `Zone:DNS:Edit` | For `{customer}.arc.<domain>` |
| 4 | **Stripe** | Product "Arc Agent" $20/mo → Payment Link | Payment Link needs zero code |

Login command for #1 (run it yourself so the token never lands in a transcript):

```
echo $GITHUB_PAT | docker login ghcr.io -u joshuamschultz --password-stdin
```

**No email provider needed** — the success page *shows* the credentials. Drops one
integration from the critical path.

## Step 2 — Registry push (blocked by #1)

```
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/joshuamschultz/arc:latest --push .
```

This is also the **first real amd64 build** — only arm64 has been built and booted. Verify a
real x86 VM before a customer touches it. (The arch-specific artifacts were checked: the
nats-server amd64 tarball + checksum exist, and PyTorch's CPU index carries x86_64 wheels.)

## Step 3 — Code (no credentials needed)

1. **Entrypoint**: provider-aware key check (currently **hardcodes `ANTHROPIC_API_KEY`** —
   picking OpenAI in the wizard would fail at boot), and accept pre-supplied
   `VIEWER_TOKEN`/`OPERATOR_TOKEN`.
2. **Provisioner** (~150 lines FastAPI): `POST /provision` → Hetzner create w/ cloud-init →
   poll `/api/health` → Cloudflare A record → return URL + tokens. Teardown on
   `customer.subscription.deleted`. **Idempotency key** so a Stripe webhook replay can't
   double-provision.
3. **Where it runs:** its own CX22, separate from customer VMs (needs a public URL for
   Stripe webhooks, must outlive any single customer).

## Step 4 — Site

Static site on Cloudflare Pages (DNS already there). Three screens: pitch + Deploy button →
wizard → success page showing live URL + token.

## Step 5 — Telegram (customer-side gotchas)

Each customer needs **his own bot** — one token = one bot, can't be shared. Wizard must collect:

1. **Bot token** — [@BotFather](https://t.me/BotFather) → `/newbot`
2. **His Telegram user ID** — [@userinfobot](https://t.me/userinfobot)

**`allowed_user_ids = []` means deny-all (fail-closed).** Omit #2 and the customer is locked
out of his own agent with no error he'd understand. Most likely first-customer bounce.

## Step 6 — Dry run, then send the link

Provision yourself. Time payment → live URL against the <8min P95 target. Message the bot.
Verify a real turn. Cancel and confirm the VM **and** DNS record are actually gone.

---

## Economics

| Item | Cost/customer/mo |
|---|---|
| Hetzner CX22 | $4.00 |
| Stripe fees | ~$0.88 |
| Cloudflare / monitoring | $0.00 |
| **COGS** | **~$4.88** against $20 |

Customer brings their own LLM key → **inference cost is theirs, not yours.** The blueprint's
`[budget] max_cost_usd` cap protects them, which is a feature to sell.

---

## Faster alternative for n=1

The PRD's own Phase 1 is manual-assisted and it's right: Stripe Payment Link + a 3-question
form + running one provision command by hand = a paying friend in **~2 days** instead of ~1
week, and it surfaces what actually breaks before automating. Steps 3–5 are unchanged after.

---

## Known-open, non-blocking

- `ruff format --check` wants **83 files** reformatted repo-wide (pre-existing drift).
- `arc agent create` still hardcodes personal tier for the *blueprint* path (the `--tier`
  flag exists and works).
- Merging `feat/quick-deploy` → `main`.
