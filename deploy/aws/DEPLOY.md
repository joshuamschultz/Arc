# Arc demo deploy — AWS Lightsail

Mirrors `deploy/azure/`. CloudFormation provisions a Lightsail VM + static IP +
firewall rules. Code goes up via `rsync` (no git on the VM, no deploy keys).
Manual push only — nothing automated, nothing fires on commit.

## Architecture

```
┌──────────────┐                           ┌──────────────────────────────┐
│  Your laptop │   1. aws cloudformation   │  AWS Lightsail               │
│              │      deploy               │                              │
│  + AWS CLI   │ ────────────────────────► │  ┌────────────────────────┐  │
│  + git/rsync │                           │  │  arc-demo (Ubuntu 22)  │  │
└──────┬───────┘   2. rsync codebase       │  │                        │  │
       │           ──────────────────────► │  │  Caddy :443 ──► UI     │  │
       │                                   │  │  arc-stack (systemd)   │  │
       │           3. ssh + setup-vm.sh    │  │   ├─ arc ui            │  │
       └─────────────────────────────────► │  │   └─ arc agent serve   │  │
                                           │  │                        │  │
   ┌──────────────────┐    HTTPS           │  │  /home/ubuntu/arc/.env │  │
   │ demo.<your-dom>  │ ───────────────────┼──┼─►   (chmod 600)        │  │
   └──────────────────┘                    │  └────────────────────────┘  │
                                           └──────────────────────────────┘
```

**Secret flow (demo-tier):** API keys live in `/home/ubuntu/arc/.env` with `chmod 600`.
For federal-grade, swap to EC2 + IAM Instance Profile + AWS Secrets Manager (TODO at
the bottom of this doc).

## Prerequisites

- `aws` CLI configured: `aws sts get-caller-identity` returns your account
- `jq` installed locally
- Domain you control (DNS editable wherever blackarcsystems.com is registered)
- An LLM API key (Anthropic recommended — `my_agent` uses it)

## Step 1 — provision the VM (5 min)

```bash
cd deploy/aws
./deploy.sh
```

Idempotent — re-run safely. It does:

1. Validates AWS auth
2. Detects your IP (informational; `parameters.json` keeps SSH open by default)
3. Validates the CloudFormation template
4. `aws cloudformation deploy` — creates the `arc-demo` stack (instance + ports)
5. **Allocates the static IP outside the stack** (idempotent — reuses
   `arc-demo-ip` if it already exists), attaches it to the instance
6. Downloads the Lightsail default keypair to `~/.ssh/lightsail-us-east-1.pem`
7. Polls SSH until reachable

Outputs the static IP, SSH command, and the runbook for the remaining 3 steps.

**Why the static IP is outside the stack:** so `aws cloudformation
delete-stack` releases the VM but keeps the IP. Your DNS A-record
(`agent.blackarcsystems.com → <ip>`) is set once and stays valid through
every redeploy. If you ever want to release the IP entirely:
`aws lightsail release-static-ip --static-ip-name arc-demo-ip --region us-east-1`.

## Step 2 — point DNS at the static IP (2 min)

In your DNS provider:

```
A   demo.blackarcsystems.com   →   <static IP from step 1>   TTL 300
```

Verify before continuing:

```bash
dig +short demo.blackarcsystems.com
# must print the static IP, or Caddy's Let's Encrypt request will fail
```

## Step 3 — push the codebase (60–90 sec)

The exact rsync command is printed by `deploy.sh`. It looks like:

```bash
rsync -avz --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.arc-logs' \
  --exclude='node_modules' \
  --exclude='*.pyc' \
  -e "ssh -i ~/.ssh/lightsail-us-east-1.pem -o StrictHostKeyChecking=accept-new" \
  /path/to/arc/ ubuntu@<static-ip>:/home/ubuntu/arc/
```

**No git on the VM.** This is intentional — keeps the deploy boundary explicit.
You decide when to push by re-running `rsync`. There's no webhook, no Action,
no CI rebuilding the VM behind your back.

## Step 3.5 — copy agent identity keys (one-time, ~10 sec)

Each agent in `team/<name>/arcagent.toml` has a pinned DID like
`did:arc:local:executor/79430140`. The matching keypair lives at
`~/.arcagent/keys/<did-flattened>.{key,pub}` on your laptop and is **not** in
the repo (private key — never commit). Without it, the agent crashes with
`Key file not found` at startup.

From your laptop, copy the keys for the agents you're enabling:

```bash
# extract the DID hashes for the agents you want
DIDS=$(grep -h '^did =' \
  ~/Projects/arc/team/nlit_cora_agent/arcagent.toml \
  ~/Projects/arc/team/nlit_soc_agent/arcagent.toml \
  ~/Projects/arc/team/scap_isso_agent/arcagent.toml \
  | sed -E 's/.*"did:arc:[^/]+\/([^"]+)".*/\1/')

# rsync just those keypairs (and nothing else from ~/.arcagent/)
ssh -i ~/.ssh/lightsail-us-east-1.pem ubuntu@<static-ip> \
    'mkdir -p ~/.arcagent/keys && chmod 700 ~/.arcagent'
for d in $DIDS; do
  rsync -az -e "ssh -i ~/.ssh/lightsail-us-east-1.pem" \
    ~/.arcagent/keys/did_arc_*_${d}.{key,pub} \
    ubuntu@<static-ip>:~/.arcagent/keys/
done
ssh -i ~/.ssh/lightsail-us-east-1.pem ubuntu@<static-ip> \
    'chmod 600 ~/.arcagent/keys/*.key && chmod 644 ~/.arcagent/keys/*.pub'
```

## Step 4 — SSH in and run `setup-vm.sh` (10–15 min)

```bash
ssh -i ~/.ssh/lightsail-us-east-1.pem ubuntu@<static-ip>

# First time on this VM:
nano ~/arc/.env                                   # ANTHROPIC_API_KEY=sk-ant-...
bash ~/arc/deploy/aws/setup-vm.sh demo.blackarcsystems.com
# (no agent args ⇒ defaults to: nlit_cora_agent + nlit_soc_agent + scap_isso_agent)
```

`setup-vm.sh`:

1. Installs Python 3.11, build tools, Caddy
2. Installs uv and runs `uv sync` (the slow part — `small_3_0` does it in ~6 min)
3. Verifies `.env` has at least one provider key
4. Pre-warms `~/.arcagent/arc-stack.tokens` so the demo URL stays stable
5. Disables agents you didn't pass (rename-based, reversible)
6. Installs `arc-stack.service` (systemd) and starts it
7. Installs the Caddyfile, reloads Caddy → Let's Encrypt issues the cert
8. Prints the demo URL with the embedded viewer token

Pass multiple agents to keep more enabled:

```bash
bash ~/arc/deploy/aws/setup-vm.sh demo.blackarcsystems.com my_agent nlit_cora_agent
```

## Step 4.5 — Slack adapter setup (operator manual steps)

This step enables the Slack fallback channel for all three demo agents
(`scap_isso_agent`, `nlit_cora_agent`, `nlit_soc_agent`). Each agent's
`arcagent.toml` already has `[platforms.slack]` enabled and reads tokens
from the env vars below. The operator must complete steps B1 and B4 before
the Slack path is live.

### B1 — Provision the Slack workspace and bot (manual)

1. Create a Slack workspace (or use an existing one you control).
2. Go to https://api.slack.com/apps → **Create New App** → **From scratch**.
3. Under **OAuth & Permissions**, add these **Bot Token Scopes**:
   - `chat:write` — post messages as the bot
   - `app_mentions:read` — receive @-mentions in channels
   - `im:history` — read DMs sent to the bot
   - `im:write` — open DM channels to users
4. Under **Socket Mode**, enable Socket Mode and generate an **App-Level Token**
   (`xapp-…`) with the `connections:write` scope.
5. Install the app to your workspace. Copy the **Bot User OAuth Token** (`xoxb-…`)
   from the OAuth & Permissions page.
6. Invite the bot to the relevant channels: `/invite @<bot-name>`.

### B4 — Add tokens to `.env` on the demo VM (manual)

SSH into the demo VM and append the Slack tokens to `.env`:

```bash
ssh -i ~/.ssh/lightsail-us-east-1.pem ubuntu@<static-ip>
nano ~/arc/.env
```

Add (replacing the placeholder values with your real tokens):

```
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
SLACK_APP_TOKEN=xapp-your-app-token-here
```

Then lock the file:

```bash
chmod 600 ~/arc/.env
```

Never commit `.env` or print its contents in logs.

### B4.5 — Populate `allowed_user_ids` on each agent (REQUIRED)

The TOML configs ship with `allowed_user_ids = []`. **Empty means deny-all**
(slack.py D-016). Until populated, the SlackAdapter rejects every inbound
message from every Slack user. Get your Slack member ID from your profile
page (a string like `U01ABCDEF`), then edit each agent's TOML:

```bash
nano ~/arc/team/scap_isso_agent/arcagent.toml
# replace allowed_user_ids = []  →  allowed_user_ids = ["U01ABCDEF", ...]
# repeat for nlit_cora_agent and nlit_soc_agent
```

Restart arc-stack after editing so the new allow-list takes effect.

### Verify both adapters registered

After restarting `arc-stack`, check the startup log for both adapter lines:

```bash
sudo systemctl restart arc-stack
journalctl -u arc-stack -n 50 | grep -E "web_adapter|slack_adapter|registering"
```

You should see output like:

```
bootstrap: embedded gateway built (tier=personal web=True slack=True telegram=False)
```

A `web=True slack=True` line confirms both adapters registered for the agent.
If `slack=False`, check that `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set
in `.env` and that `arc-stack` was restarted after the edit.

### B8 — Manual rehearsal (manual)

Kill the arcui process, then open Slack and DM `@<bot-name>`. Verify:
- The bot replies in Slack.
- The audit log (`~/arc/.arc-logs/*.log`) shows `platform=slack` events.

This confirms AC-2.3: killing arcui leaves Slack chat fully functional.

## Step 4.6 — Mattermost adapter setup (air-gapped / federal deployments)

The Mattermost adapter provides a FedRAMP High-eligible chat surface for
DOE/National Lab evaluators who run in air-gapped environments (FedHIVE /
IL5 / JWICS). It requires an on-premises Mattermost Team Edition or Enterprise
server reachable from the arc-stack VM.

**When to use:** DOE Sandia / national lab evaluations where Slack and Telegram
are unavailable. For commercial demos, Slack (§4.5) is simpler.

**Session-key note (ADR-002):** A conversation started in Mattermost and
continued in arcui will show as two separate sessions with separate histories.
Cross-platform unified history is deferred to SPEC-026. Document this in your
demo runbook for the evaluator.

### MM1 — Install aiohttp on the VM (one-time)

The Mattermost adapter depends on `aiohttp`, which is not installed by default.

```bash
ssh -i ~/.ssh/lightsail-us-east-1.pem ubuntu@<static-ip>
cd ~/arc
uv pip install 'arcgateway[mattermost]'
```

### MM2 — Create a Mattermost bot account (on the MM server)

1. In your Mattermost server, go to **System Console → Integrations → Bot Accounts**.
2. Enable bot accounts if not already enabled.
3. Create a new bot: give it a username (e.g. `arc-isso`) and note the **Bot User ID**.
4. Go to the bot's profile → **Security** → **Personal Access Tokens** → **Create**.
   Copy the token (displayed once — save it securely).
5. Add the bot to the channel(s) where operators will chat with the agent.
6. Note the **Channel ID(s)**: System Console → Channels → select channel → copy ID
   from the URL (`/channels/<CHANNEL_ID>`).

### MM3 — Add the PAT to `.env` on the VM

```bash
ssh -i ~/.ssh/lightsail-us-east-1.pem ubuntu@<static-ip>
nano ~/arc/.env
```

Add:

```
MM_BOT_TOKEN=your-mattermost-personal-access-token
```

Then:

```bash
chmod 600 ~/arc/.env
```

Never commit `.env` or echo its contents.

### MM4 — Enable the adapter in each agent's `arcagent.toml`

```bash
nano ~/arc/team/scap_isso_agent/arcagent.toml
```

Add (or uncomment) the `[platforms.mattermost]` block:

```toml
[platforms.mattermost]
enabled = true
server_url = "https://mattermost.internal.doe.gov"   # your MM server URL
bot_token_env = "MM_BOT_TOKEN"
allowed_channel_ids = ["<CHANNEL_ID_1>", "<CHANNEL_ID_2>"]
bot_user_id = "<BOT_USER_ID>"
# intranet_domains required at federal tier if MM URL does not resolve to RFC 1918:
intranet_domains = ["mattermost.internal.doe.gov"]
```

Repeat for `nlit_cora_agent` and `nlit_soc_agent`.

**Federal-tier air-gap enforcement:** When `[gateway] tier = "federal"`, the
adapter validates at startup that `server_url` resolves to RFC 1918 (10/8,
172.16/12, 192.168/16), loopback, or a hostname in `intranet_domains`. Any
public DNS hostname raises `ValueError` and refuses to start. This prevents
accidental phone-home to an internet-reachable Mattermost in an air-gapped
environment (SPEC-025 §NFR-5).

### MM5 — Restart arc-stack and verify

```bash
sudo systemctl restart arc-stack
journalctl -u arc-stack -n 60 | grep -E "mattermost|bootstrap"
```

Expected output:

```
bootstrap: embedded gateway built (tier=personal web=True slack=True telegram=False mattermost=True)
MattermostAdapter: connecting to https://mattermost.internal.doe.gov (tier=personal)
MattermostAdapter: WS connected to wss://mattermost.internal.doe.gov/api/v4/websocket
```

If `mattermost=False`: verify `MM_BOT_TOKEN` is set in `.env`, `server_url` is
populated in the TOML, and `arc-stack` was restarted after the edit.

### MM6 — Manual rehearsal (manual)

Open Mattermost, navigate to the configured channel, and DM the bot (`@arc-isso`).
Verify:
- The bot replies in Mattermost.
- The audit log shows `platform=mattermost` events:
  ```bash
  grep 'platform.*mattermost' ~/arc/.arc-logs/*.log | tail -5
  ```

## Step 5 — open the URL

`setup-vm.sh` prints something like:

```
https://demo.blackarcsystems.com/#auth=<viewer-token>
```

That's the demo URL. The token in the fragment grants viewer access. Anyone with
the link can see ArcUI — fine for a demo, just don't post it publicly.

## Day-2 ops (phone-runnable via Terminus)

| Task | Command |
|------|---------|
| Restart everything | `sudo systemctl restart arc-stack caddy` |
| Watch agent logs | `tail -f ~/arc/.arc-logs/*.log` |
| Watch UI service | `journalctl -u arc-stack -f` |
| Reload Caddy / TLS | `sudo systemctl reload caddy` |
| Get the demo URL again | `cat ~/.arcagent/arc-stack.tokens` |
| Re-enable an agent | `mv ~/arc/team/<name>_agent.disabled ~/arc/team/<name>_agent && sudo systemctl restart arc-stack` |
| Push fresh code (from laptop) | re-run the rsync from step 3 |

## Cost

| Resource | Monthly |
|---|---|
| Lightsail `small_3_0` (2GB / 2vCPU) | $12 |
| Static IP (while attached) | $0 |
| Egress (3 TB included) | $0 |
| TLS cert (Let's Encrypt) | $0 |
| **Total** | **~$12** |

Stop the instance via Lightsail console when the demo's over. Or full teardown:

```bash
aws cloudformation delete-stack --stack-name arc-demo --region us-east-1
```

That deletes the VM, static IP, and firewall rules. Nothing left running.

## Files

| File | Role | Mirrors |
|---|---|---|
| `main.yaml` | CloudFormation template (VM + static IP + ports) | `azure/main.bicep` |
| `parameters.json` | parameter overrides | `azure/parameters.json` |
| `deploy.sh` | provisioning wrapper (CFN deploy + key download + runbook print) | `azure/deploy.sh` |
| `setup-vm.sh` | VM-side setup (deps + uv sync + systemd + Caddy + start) | `azure/setup-vm.sh` |
| `arc-stack.service` | systemd unit wrapping `scripts/arc-stack.sh` | (Azure has per-agent units) |
| `Caddyfile` | reverse proxy 127.0.0.1:8420 with TLS + WS + JSON access logs | (Azure runs nginx via setup-vm) |
| `DEPLOY.md` | this file | `azure/DEPLOY.md` |

## Choices made (skim if curious)

- **Lightsail over EC2**: flat pricing, public IP + DNS in one console screen.
  EC2 + VPC + IGW + SG + EBS + EIP is busywork for a demo. Migrate to EC2 if/when
  you need IAM Instance Profile + Secrets Manager (federal posture).
- **CloudFormation over Terraform**: native AWS, no extra install, matches the
  "Azure has Bicep, AWS has CFN" symmetry.
- **Caddy over nginx**: auto Let's Encrypt with three lines. No certbot cron.
- **systemd over Docker**: `arc-stack.sh` already manages multiple processes
  correctly. Containerizing for one box is overhead.
- **rsync over `git pull` on the VM**: deploy boundary is explicit. You push
  when you push. No deploy key on the VM, no chance of GitHub auth breaking
  the deploy at 2am.
- **Disable-by-rename**: `arc-stack.sh` starts every `*_agent` dir under
  `TEAM_ROOT` when given no args. Renaming filters cleanly and survives `uv sync`.

## Production secrets via AWS Secrets Manager

The demo path stores LLM API keys in `/home/ubuntu/arc/.env` (chmod 600). For
production, swap to `AwsSecretsManagerBackend` so the keys live in AWS Secrets
Manager and are resolved via the instance's IAM role — no plaintext keys on disk.

The backend module is `arcllm.backends.aws_secrets:AwsSecretsManagerBackend`.
It uses boto3's default credential chain (IAM Instance Profile → `~/.aws/credentials`
→ `AWS_ACCESS_KEY_ID` env vars). `setup-vm.sh` already `pip install`s boto3.

### 1. Store the keys in Secrets Manager

```bash
aws secretsmanager create-secret \
  --name arc/prod/anthropic/api_key \
  --secret-string "sk-ant-..." \
  --region us-east-1
aws secretsmanager create-secret \
  --name arc/prod/openai/api_key \
  --secret-string "sk-..." \
  --region us-east-1
```

Convention: `arc/<env>/<service>/<key>`. The backend does **not** prefix paths
automatically — operators choose any IAM-policy-aligned scheme.

### 2. Attach this IAM policy to the instance role

Lightsail does not support instance roles — provision an EC2 instance instead
(see the "federal posture" TODO at the bottom). Once the role exists, attach:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ArcLLMReadSecrets",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:arc/*"
    }
  ]
}
```

Resource-scoped to `arc/*` — the role cannot read any other secret in the
account. Replace `<ACCOUNT_ID>` with your AWS account number.

### 3. Wire the backend in `arcagent.toml`

```toml
[llm.vault]
backend = "arcllm.backends.aws_secrets:AwsSecretsManagerBackend"
region_name = "us-east-1"

[llm]
provider = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"   # fallback if vault is unreachable
vault_path = "arc/prod/anthropic/api_key"
```

`VaultResolver` (in arcllm) handles TTL caching and env-var fallback. If the
backend is unavailable (no boto3, no creds, AWS unreachable) it transparently
falls back to `os.environ[ANTHROPIC_API_KEY]`. The only fail-loud case is
`AccessDeniedException` — that surfaces as `ArcLLMConfigError` so a misconfigured
IAM policy can't silently route to the env-var path.

### 4. Remove the keys from `.env`

Once the agent boots cleanly with `vault_path` set, delete the corresponding
`*_API_KEY` lines from `/home/ubuntu/arc/.env`. The vault path is now the only
source of truth.

## TODO — federal posture (if/when this stops being a demo)

Replace this stack with EC2-based:

1. Replace `AWS::Lightsail::Instance` with `AWS::EC2::Instance` + an `AWS::IAM::Role`
   that has the `secretsmanager:GetSecretValue` policy above
2. Replace open-to-internet SSH with AWS Systems Manager Session Manager
3. Add CloudWatch Log groups for arc-stack + Caddy logs

That's a real chunk of work — leave it until the demo proves it's worth doing.
