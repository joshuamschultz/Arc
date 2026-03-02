# ArcAgent Security Hardening Runbook

Operational steps for hardening an ArcAgent deployment beyond what the code enforces automatically. These are deployment-time and infrastructure-level actions.

---

## 1. File System Permissions

### Agent Key Directory

The agent identity keys are stored at `~/.arcagent/keys/` by default. The code sets `0o700` on the directory and `0o600` on key files, but verify after deployment:

```bash
# Verify key directory permissions
stat -f "%A %N" ~/.arcagent/keys/
# Expected: 700

# Verify key files
stat -f "%A %N" ~/.arcagent/keys/*.key
# Expected: 600

# Verify DID persistence files
stat -f "%A %N" ~/.arcagent/keys/*.did
# Expected: 600

# Verify public key files
stat -f "%A %N" ~/.arcagent/keys/*.pub
# Expected: 644
```

### Trace Store Directory

Trace JSONL files are stored at `{workspace}/traces/`. The code sets `0o700` on the directory and `0o600` on JSONL files, but verify:

```bash
# Verify traces directory
stat -f "%A %N" /path/to/workspace/traces/
# Expected: 700

# Verify trace files
stat -f "%A %N" /path/to/workspace/traces/*.jsonl
# Expected: 600
```

### Agent Workspace

The workspace directory contains agent state, memory, and tasks. Restrict access:

```bash
chmod 700 /path/to/agent/workspace
chmod -R go-rwx /path/to/agent/workspace
```

---

## 2. Environment Variables

### Required Secrets

Never put API keys in TOML config files. Set via environment:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
# Or use vault backend — see arcllm security docs
```

### Telegram Bot Token

If Telegram module is enabled:

```bash
export TELEGRAM_BOT_TOKEN=...
```

### Verify No Secrets in Config

```bash
# Check for hardcoded secrets in TOML files
grep -r "sk-\|api_key\s*=\s*\"[^\"]\+" /path/to/agent/*.toml
# Should return nothing
```

---

## 3. Identity Verification

### Verify DID Persistence

After first agent startup, verify the DID was persisted:

```bash
cat ~/.arcagent/keys/local_executor.did
# Should show: did:arc:local:executor/xxxxxxxx
```

On subsequent restarts, the same DID should be used:

```bash
# Start agent, note the DID in logs
# Stop agent
# Start agent again
# Verify same DID appears in logs
```

### Verify Hash Chain Integrity

Run periodically or after suspected tampering:

```python
from arcllm.trace_store import JSONLTraceStore
import asyncio

store = JSONLTraceStore(Path("/path/to/workspace"))
result = asyncio.run(store.verify_chain())
assert result is True, "Hash chain integrity check failed — possible tampering"
```

---

## 4. Network Hardening

### ArcUI Dashboard

The UI binds to `127.0.0.1` by default (localhost only). For remote access:

- Use an SSH tunnel or reverse proxy with TLS
- Never expose the UI directly to the internet
- The viewer token is auto-generated (64 hex chars) — treat it as a secret

### NATS Message Bus

If inter-agent messaging is enabled:

- Enable TLS on NATS connections
- Use credential-based auth (not open access)
- Restrict to agent-specific subjects

---

## 5. Audit Log Rotation

Trace JSONL files rotate daily (`traces-YYYY-MM-DD.jsonl`). For long-running deployments:

- Archive old trace files to secure storage
- Verify chain integrity before archiving
- Retain per compliance requirements (FedRAMP: minimum 1 year)
- Do not modify archived files — chain will break

---

## 6. Compliance Checklist

| Control | Requirement | Verification |
|---------|-------------|--------------|
| NIST AU-9 | Protect audit information | `stat` on traces dir/files shows `700`/`600` |
| NIST AU-10 | Non-repudiation | `verify_chain()` returns True |
| NIST IA-5 | Authenticator management | Key files are `0o600`, no secrets in config |
| NIST SI-10 | Input validation | Provider names match `[a-z][a-z0-9_]` |
| NIST AC-6 | Least privilege | Agent workspace owned by dedicated user |
| FedRAMP AU-9(3) | Cryptographic protection | SHA-256 hash chain on all trace records |
