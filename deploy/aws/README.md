# Arc demo deploy — AWS Lightsail

Single VM, Caddy in front, systemd-managed agent stack. Designed so the
final hop (after `git clone`) is one command you can paste from a phone.

## What you'll have at the end

```
https://demo.blackarcsystems.com/#auth=<token>
```

A public HTTPS URL serving ArcUI, with the agent(s) running behind it.

## 0. Prereqs (5 min, on laptop)

- AWS account with Lightsail access
- DNS for `blackarcsystems.com` reachable from wherever you manage records
- An LLM API key (Anthropic recommended for the simplest agent — `my_agent`)

## 1. Provision Lightsail (5 min, in AWS console)

1. Lightsail → **Create instance**
2. Region: `us-east-1` (or closest)
3. Platform: **Linux/Unix** → Blueprint: **OS Only → Ubuntu 22.04 LTS**
4. Plan: **$12/mo (2 GB / 2 vCPU / 60 GB SSD)** — smaller will OOM under agent load
5. Identify it: `arc-demo`
6. **Create instance**

Then:

7. Click the instance → **Networking** → **+ Add rule**
   - Allow **HTTP** (80)
   - Allow **HTTPS** (443)
   - Leave **SSH** (22) as-is
8. **Networking** tab → **Create static IP** → attach to `arc-demo` (free while attached)
9. **Connect** tab → **Download default key** (`LightsailDefaultKey-us-east-1.pem`) — save it; you'll load this into Terminus on iPhone

## 2. Point DNS at the static IP (2 min)

In your DNS provider for `blackarcsystems.com`:

```
Type: A
Name: demo            (→ demo.blackarcsystems.com)
Value: <static IP from step 1.8>
TTL: 300
```

Verify before moving on:

```bash
dig +short demo.blackarcsystems.com
# should print the static IP
```

## 3. SSH in (Terminus on iPhone or laptop)

- Host: `<static IP>` or `demo.blackarcsystems.com`
- User: `ubuntu`
- Key: the `.pem` from step 1.9 (Terminus → Keychain → Import)

## 4. On the VM — generate a deploy key for GitHub (2 min)

The Arc repo is private, so the VM needs a GitHub deploy key:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy -C "arc-demo-vm"
cat ~/.ssh/github_deploy.pub
```

Copy that output. In GitHub:

- Go to `joshuamschultz/Arc` → **Settings → Deploy keys → Add deploy key**
- Title: `arc-demo-vm`
- Paste the pubkey
- **Read access is enough** (don't tick "Allow write access")
- **Add key**

Now wire it into SSH config on the VM:

```bash
cat >>~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T -o StrictHostKeyChecking=accept-new git@github.com || true
```

The last line should say "Hi joshuamschultz/Arc! You've successfully authenticated".

## 5. Clone and run install.sh (10–15 min)

```bash
git clone git@github.com:joshuamschultz/Arc.git ~/arc
cd ~/arc
bash deploy/aws/install.sh demo.blackarcsystems.com my_agent
```

First run will exit with a message about `.env`. Edit it:

```bash
nano ~/arc/.env
# set ANTHROPIC_API_KEY=sk-ant-...   (my_agent uses Anthropic)
```

Then re-run:

```bash
bash deploy/aws/install.sh demo.blackarcsystems.com my_agent
```

The script:

- Installs Python 3.11, uv, Caddy
- `uv sync` to build the workspace venv (`.venv/bin/arc`)
- Disables agents you didn't pass (renames `*_agent` → `*_agent.disabled`)
- Installs `arc-stack.service` and starts it
- Installs the Caddyfile and reloads Caddy (auto-provisions Let's Encrypt cert)
- Prints the demo URL with the embedded viewer token

## 6. Verify

```bash
curl -fsS http://127.0.0.1:8420/api/health      # should return JSON
curl -fsSI https://demo.blackarcsystems.com/    # should be 200 with HSTS
sudo systemctl status arc-stack caddy
```

Open the URL the script printed. You should see ArcUI with the agent listed.

## Day-2 ops (everything is phone-runnable)

| Task | Command |
|------|---------|
| Restart everything | `sudo systemctl restart arc-stack` |
| Watch agent logs | `tail -f ~/arc/.arc-logs/*.log` |
| Watch UI logs | `journalctl -u arc-stack -f` |
| Reload TLS / Caddy | `sudo systemctl reload caddy` |
| Get the demo URL again | `cat ~/.arcagent/arc-stack.tokens && echo demo.blackarcsystems.com` |
| Re-enable an agent | `mv ~/arc/team/<name>_agent.disabled ~/arc/team/<name>_agent && sudo systemctl restart arc-stack` |
| Pull latest code | `cd ~/arc && git pull && uv sync && sudo systemctl restart arc-stack` |

## Cost

- Lightsail 2GB instance: **$12/mo**
- Static IP: free while attached
- Egress: 3 TB included
- TLS cert: free (Let's Encrypt via Caddy)

Total: **~$12/mo**. Stop the instance when the demo's over to drop to ~$0.

## Tearing it down

In Lightsail console → instance → **Stop** (preserves disk, no compute charge), or
**Delete** (removes everything). Detach the static IP first if you want to keep it.

## Files in this directory

- `install.sh` — the bootstrap script that runs on the VM
- `arc-stack.service` — systemd unit
- `Caddyfile` — Caddy reverse-proxy config (DEMO_DOMAIN gets sed-replaced)
- `README.md` — this file

## Why these choices (skim if curious)

- **Lightsail over EC2**: flat pricing, public IP + DNS in one console screen. EC2 + VPC + IGW is busywork for a demo.
- **Caddy over nginx**: auto Let's Encrypt with one line. No certbot cron.
- **systemd over Docker**: `arc-stack.sh` already manages multiple processes correctly. Containerizing for one box is overhead.
- **Disable-by-rename for agents**: arc-stack.sh starts every `*_agent` dir under `team/` when given no args. Renaming is the simplest filter that survives `git pull`.
