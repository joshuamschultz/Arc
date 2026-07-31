# Scoped Secrets for Extensions

> One call. Extensions never know where secrets come from.

---

## API

```python
val = api.get_secret("my-secret-name")
```

That's it. Extensions call `get_secret()` with a secret name and receive the value or `None`.

---

## Resolution Order

```
1. Vault backend  (if [vault] configured in arcagent.toml)
2. Environment variable  (name uppercased, hyphens → underscores)
```

| Secret Name | Vault Lookup | Env Var Fallback |
|-------------|-------------|------------------|
| `atlassian-base-site` | Azure Key Vault key `atlassian-base-site` | `ATLASSIAN_BASE_SITE` |
| `ctg-federal-atlassian-api-key` | Azure Key Vault key `ctg-federal-atlassian-api-key` | `CTG_FEDERAL_ATLASSIAN_API_KEY` |
| `jira-email` | Azure Key Vault key `jira-email` | `JIRA_EMAIL` |

---

## Configuration

### Production (vault-backed)

```toml
# arcagent.toml
[vault]
backend = "azure_keyvault"
vault_url = "https://your-vault.vault.azure.net"
```

Secrets resolve via `DefaultAzureCredential` (Managed Identity on Azure VMs, CLI creds locally). No filesystem credentials.

### Development (env var fallback)

If no `[vault]` section exists in `arcagent.toml`, secrets fall back to environment variables automatically. No code change needed.

```bash
export ATLASSIAN_BASE_SITE="https://your-org.atlassian.net"
export JIRA_EMAIL="you@example.com"
export CTG_FEDERAL_ATLASSIAN_API_KEY="your-api-token"
```

---

## How It Works

### Extension Side

Extensions receive an `ExtensionAPI` instance. They call `get_secret()` and handle `None` (missing) gracefully:

```python
def extension(api):
    base_url = api.get_secret("atlassian-base-site")
    if not base_url:
        logger.error("Missing secret: atlassian-base-site")
        return  # Extension disabled, agent continues without it
    # ... use secret
```

### Framework Side

`ExtensionAPI.get_secret()` is the unified resolver. The vault backend (if any) is threaded from:

```
ArcAgent._vault_resolver
  → ExtensionLoader(vault_resolver=...)
    → ExtensionAPI(vault_resolver=...)
      → get_secret()  # vault first, then env
```

Source: `arcagent/core/extensions.py` — `ExtensionAPI.get_secret()`

---

## Security Properties

| Property | How |
|----------|-----|
| **No secrets on disk** | Vault-backed resolution via Managed Identity. No `.env` files in production. |
| **Least privilege** | Extensions only access secrets they explicitly request by name. No bulk access. |
| **Audit trail** | Failed lookups logged with extension name, secret name, and resolution path. |
| **Graceful degradation** | Missing secrets disable the extension, not the agent. Other extensions load normally. |
| **Env fallback for dev** | Developers can use env vars locally without vault infrastructure. |
| **Name convention** | Hyphenated vault names map to `UPPER_SNAKE` env vars automatically. |

---

## Compliance Mapping

| Requirement | Control |
|-------------|---------|
| NIST 800-53 IA-5 | Credentials vault-managed, not filesystem-stored |
| NIST 800-53 AU-3 | Secret access attempts logged with context |
| FedRAMP SC-12 | Cryptographic key management via Azure Key Vault HSM |
| CMMC L3 3.13.10 | Key management procedures enforced by vault backend |

---

## Adding New Secrets

1. Store the secret in Azure Key Vault (hyphenated name: `my-new-secret`)
2. In your extension, call `api.get_secret("my-new-secret")`
3. Handle `None` return — disable gracefully if the secret is required
4. Document the secret name in your extension's docstring or README

No framework changes needed. No configuration changes needed. The convention handles routing.
