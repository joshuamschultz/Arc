# Azure OpenAI Setup Runbook

Setting up ArcLLM with Azure OpenAI Service (Azure AI Foundry) for commercial and GCC environments.

---

## Prerequisites

- An Azure subscription with access to Azure OpenAI Service
- A deployed model in Azure AI Foundry (formerly Azure OpenAI Studio)
- The deployment name, resource name, and API key

---

## Step 1: Create an Azure OpenAI Resource

1. Go to [Azure Portal](https://portal.azure.com) (or [Azure Government Portal](https://portal.azure.us) for GCC)
2. Search for **Azure OpenAI** and create a new resource
3. Select your subscription, resource group, and region
4. Note your **resource name** — this becomes part of the endpoint URL

**Endpoint format:**

| Environment | Endpoint |
|-------------|----------|
| Commercial | `https://{resource-name}.openai.azure.com` |
| GCC | `https://{resource-name}.openai.azure.us` |

---

## Step 2: Deploy a Model

1. Open [Azure AI Foundry](https://ai.azure.com) (or the GCC equivalent)
2. Navigate to your resource and go to **Deployments**
3. Click **Create deployment**
4. Select a model (e.g., `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`)
5. Choose a **deployment name** — this is what you pass to `load_model()`

**Important:** New deployments take up to 5 minutes to propagate. You may see 404 `DeploymentNotFound` errors immediately after creation. This is normal.

### GCC Available Models

| Model | Context Window | Max Output |
|-------|---------------|------------|
| gpt-4o | 128K | 16K |
| gpt-4o-mini | 128K | 16K |
| gpt-4.1 | 300K | 32K |
| gpt-4.1-mini | 300K | 32K |

**Note:** GCC context windows max at ~300K tokens (not 1M like commercial Azure).

---

## Step 3: Get Your API Key

1. In Azure Portal, navigate to your Azure OpenAI resource
2. Go to **Keys and Endpoint** under Resource Management
3. Copy **Key 1** or **Key 2**

---

## Step 4: Configure Environment Variables

```bash
# Required: your API key
export AZURE_OPENAI_API_KEY="your-key-here"

# Required: your resource endpoint (override the TOML placeholder)
export ARCLLM_AZURE_OPENAI__BASE_URL="https://your-resource.openai.azure.us"
```

For GCC environments, the endpoint ends in `.azure.us`. For commercial Azure, use `.azure.com`.

---

## Step 5: Use in Code

```python
from arcllm import load_model, Message

# Load with your deployment name (not the model name)
model = load_model("azure_openai", "my-gpt4o-deployment")

response = await model.invoke([
    Message(role="user", content="What is 2 + 2?")
])

print(response.content)       # "4"
print(response.stop_reason)   # "end_turn"
```

### With ArcAgent

In your `arcagent.toml`:

```toml
[llm]
model = "azure_openai/my-gpt4o-deployment"
```

---

## How It Works

Azure OpenAI uses an OpenAI-compatible API with three key differences:

| Aspect | Standard OpenAI | Azure OpenAI |
|--------|----------------|--------------|
| **URL** | `{base}/v1/chat/completions` | `{base}/openai/v1/chat/completions` |
| **Auth header** | `Authorization: Bearer {key}` | `api-key: {key}` |
| **Model field** | Canonical name (`gpt-4o`) | Deployment name (`my-gpt4o-deployment`) |

ArcLLM's `Azure_OpenaiAdapter` handles all three transparently.

### API Version

ArcLLM uses the Azure v1 API (GA since August 2025). This endpoint does **not** require an `api-version` query parameter. In fact, appending `?api-version=` causes a 400 error.

The legacy deployment-based API (`/openai/deployments/{name}/chat/completions?api-version=2024-10-21`) is not currently supported.

---

## Azure Content Safety

Azure Content Safety may filter requests or responses. ArcLLM handles all three scenarios:

| Scenario | HTTP Status | Behavior |
|----------|------------|----------|
| **Prompt blocked** | 400 | `ArcLLMAPIError` raised with status 400 |
| **Output blocked** | 200 | `response.content` is `None`, `response.stop_reason` is `"content_filter"` |
| **Filter unavailable** | 200 | Content returned normally (filter error logged by Azure, not the adapter) |

### Handling content filter in your agent loop

```python
response = await model.invoke(messages)

if response.stop_reason == "content_filter":
    # Output was blocked by Azure Content Safety
    # response.content is None
    print("Response filtered by Azure Content Safety")
elif response.stop_reason == "end_turn":
    print(response.content)
```

---

## Rate Limits

Azure rate limits are **per-deployment** (not per-resource). Limits are measured in:

- **TPM** (Tokens Per Minute)
- **RPM** (Requests Per Minute)

When rate limited, Azure returns HTTP 429 with a `Retry-After` header. ArcLLM's `ArcLLMAPIError` captures this value in `error.retry_after`.

**Tip:** Setting unnecessarily high `max_tokens` in requests causes Azure to pre-allocate capacity against your TPM limit. Use realistic values.

---

## Troubleshooting

### 404 DeploymentNotFound

**Cause:** New deployments take up to 5 minutes to propagate.

**Fix:** Wait 5 minutes after creating a deployment before making requests.

### 400 Bad Request (api-version)

**Cause:** Something is appending `?api-version=` to the URL.

**Fix:** ArcLLM's v1 API path does not use query parameters. Verify your `base_url` doesn't include query params.

### 401 Unauthorized

**Cause:** Missing or invalid API key.

**Fix:** Verify `AZURE_OPENAI_API_KEY` is set and the key is valid. The header is case-sensitive — ArcLLM sends `api-key` (lowercase) as required by Azure.

### Response content is None

**Cause:** Azure Content Safety blocked the output.

**Fix:** Check `response.stop_reason` — if it's `"content_filter"`, the response was filtered. Review your prompt for policy violations.

---

## Security Notes

- API keys are resolved from `AZURE_OPENAI_API_KEY` environment variable or vault backend — never from config files
- All Azure endpoints use HTTPS (enforced by ArcLLM's `ProviderSettings` validator)
- GCC endpoints (`.azure.us`) are FedRAMP High authorized
- The `api-key` header is standard Azure cognitive services authentication — equivalent security to Bearer tokens
- For production GCC deployments, consider Azure Managed Identity auth (not yet supported — requires `azure-identity` dependency)
