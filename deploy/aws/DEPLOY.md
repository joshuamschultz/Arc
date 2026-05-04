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
   │ (DNS A record)   │                    │  └────────────────────────┘  │
   └──────────────────┘                    └──────────────────────────────┘
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
bash ~/arc/deploy/aws/setup-vm.sh demo.blackarcsystems.com nlit_cora_agent nlit_soc_agent
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

## TODO — federal posture (if/when this stops being a demo)

Replace this stack with EC2-based:

1. Replace `AWS::Lightsail::Instance` with `AWS::EC2::Instance` + an `AWS::IAM::Role`
   that has `secretsmanager:GetSecretValue` on a specific ARN
2. Move LLM keys from `.env` to AWS Secrets Manager
3. Have arcllm read from Secrets Manager via boto3 + IMDSv2 instead of env vars
   (matches what `arcagent.modules.vault_azure:AzureKeyVaultBackend` does for Azure)
4. Replace open-to-internet SSH with AWS Systems Manager Session Manager
5. Add CloudWatch Log groups for arc-stack + Caddy logs

That's a real chunk of work — leave it until the demo proves it's worth doing.
