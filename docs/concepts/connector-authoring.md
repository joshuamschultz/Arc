# Connector authoring contract

A connector attaches an external system to an agent by implementing the
`SourceAdapter` seam (`arcagent.extension.source.SourceAdapter`). The seam has an
unwritten contract every adapter must honour, so the eleventh connector inherits
the reliability fixes the first ten earned instead of re-earning the same bugs.

The contract is codified as a runnable checker:
`arcagent.extension.authoring.assert_connector_contract`. Write a test that runs
it against your adapter (offline, with no live backend) and it will fail the
build if the adapter breaks any clause.

```python
from arcagent.extension.authoring import assert_connector_contract

async def test_my_connector_honours_the_contract() -> None:
    await assert_connector_contract(MyAdapter(), connection_id="test-conn")
```

## The four clauses

### 1. Monotonic revision

Every object a sync returns carries a **non-decreasing** `metadata["revision"]`.

ArcMemory skips an unchanged object by its content `version` and only consults the
revision when the content changed — where the new revision must be strictly
greater than the stored one. A source that emits a **decreasing** revision
therefore silently stops re-indexing: the newer object is dropped as stale.

If your source has no natural monotonic field, synthesise one. `arc_ext_dropbox`
derives `revision` from `server_modified` (`_modified_revision`); `jira` uses the
issue `updated` timestamp, falling back to the current time so a later crawl
always carries a higher revision (`_revision_for`).

```python
SourceObject(
    object_id=key,
    locator=locator,
    kind=SourceObjectKind.FILE,
    metadata={"revision": monotonic_revision},   # required, non-decreasing
)
```

### 2. Lazy client re-creation after `close_source`

After `close_source()`, a later call must **still work** — the adapter rebuilds
its client rather than holding a dead handle.

`close_source` releases connections and caches at the end of a run. The next run
reuses the same adapter instance, so any method that needs a client must rebuild
it lazily if it was torn down:

```python
async def sync_source(self, request: SyncSource) -> SyncSourcePage:
    if self._client is None:      # rebuilt after close_source
        self._client = self._build_client()
    ...

async def close_source(self) -> None:
    self._client = None
```

### 3. Typed failures

A refusal surfaces as a **typed, coded** error — never a bare `Exception` an
orchestrator cannot classify or back off from.

Raise `SourceError(SourceFailureCode, detail, retry_after=...)`. The failure code
lets the orchestrator act without knowing the vendor: `AUTH_REQUIRED` triggers
credential renewal, `RATE_LIMITED` (with `retry_after`) backs off, `TOO_LARGE` and
`NOT_FOUND` are terminal for one object, and so on.

```python
raise SourceError(SourceFailureCode.AUTH_REQUIRED, "the connection needs re-authorising")
```

Errors that leak up from the attachment layer as a typed `ArcAgentError` (for
example an `ExtensionError` when a vendor binary is missing) are tolerated as
typed today, but the connector is stronger if it catches them at the boundary and
re-raises the appropriate `SourceError` code.

### 4. Tool refusals are returned, not raised

At the `ExtensionAttachment` seam (`invoke`), a tool-level refusal is returned as
`ToolResult(outcome=ToolOutcome.ERROR)` with the failure text in `content` — it is
**not** raised. A raised exception at that seam means the wire or protocol failed;
an in-band `ERROR` result means the tool ran and reported an actionable answer the
agent can read and self-correct from. Keep the two tiers distinct.

```python
return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=detail)
```

## Why the checker passes a real bundle offline

`assert_connector_contract` is runnable with no live backend. A bundle like
`jira` reaches its backend through a vendor CLI that is absent in CI, so its
lifecycle raises only typed errors — and the checker treats a typed failure as
conformant, because the clause governs *how* a failure surfaces, not that a
fresh, un-credentialed connection must succeed. A fake that raises a bare
`RuntimeError`, returns a decreasing revision, or holds a dead client after
`close_source` is flagged.

## Authoring an MCP connector

An MCP connector attaches an external MCP server to an agent. Instead of an adapter
you maintain, the vendor's own MCP server becomes your connector — you just declare
it in a bundle (`extension.toml`) and Arc wires it.

### Overview

An MCP connector is declared with `attachment = "mcp"` in the bundle manifest.
Two transport options exist: **http** (a hosted MCP server reached over streamable
HTTP) and **stdio** (a locally spawned binary). Both transports negotiate the real
MCP protocol via the official `mcp` SDK; the handshake and wire protocol are
automatic. Arc acts as a **client** to the MCP server, so you are not writing server
code — you are declaring how an existing MCP server should be reached and credentialed.

### Transport: HTTP (hosted MCP server)

Hosted MCP (e.g., Composio) is reached over streamable HTTP with optional
authentication headers. Declare it under `[config.mcp]`:

```toml
[config.mcp]
transport = "http"
url = "https://your-vendor.example.com/mcp"   # default/pinned endpoint
credential_field = "api_key"                  # which [[secrets]] carries the credential
auth_header = "Authorization"                 # HTTP header name
auth_scheme = "Bearer"                        # scheme prefix (empty = bare token)
client_name = "arc"                           # identity reported to the server
```

**Auth options** — how the credential travels:

| Vendor | `auth_header` | `auth_scheme` | Result |
|--------|---|---|---|
| Most vendors | `Authorization` | `Bearer` (default) | `Authorization: Bearer <token>` |
| Composio | `x-api-key` | `` (empty) | `x-api-key: <token>` (bare token, no prefix) |
| Custom | any | any | `{auth_header}: {auth_scheme} {token}`.strip() |

**Operator-minted URLs** — when the vendor hands each operator a per-user endpoint:

```toml
[config.mcp]
transport = "http"
url = "https://backend.example.com/default"      # placeholder shape
url_secret_field = "mcp_url"                      # override at connect time
url_origin = "https://backend.example.com/"       # trust-boundary guard
```

When the operator connects, they paste a per-user URL into the secret `mcp_url`.
The effective URL at connect time overrides the pinned `url`, but only if it starts
with `url_origin`. This guard prevents a typo or stolen host from being substituted
for the vendor's real endpoint (trust boundary). Example: Composio mints
`https://backend.composio.dev/v3/mcp/{SERVER_ID}?user_id=...` for each operator;
the operator pastes it, and `url_origin = "https://backend.composio.dev/"` refuses
anything else.

**Resilience and tool policy** — optional overrides:

```toml
[config.mcp.resilience]
timeout_seconds = 30.0
max_attempts = 3

[config.mcp.tools.TOOL_NAME]
classification = "read_only"  # override the server's declaration
```

### Transport: stdio (spawned binary)

A locally spawned MCP server (e.g., your own binary, or a third party's CLI):

```toml
[config.mcp]
transport = "stdio"
argv = ["python", "-m", "myvendor.mcp"]        # command + args
client_name = "arc"                            # identity reported to the server

[config.mcp.resilience]
timeout_seconds = 30.0
max_attempts = 3
```

The binary is spawned as a subprocess. Its environment is **scrubbed** of loader
variables (REQ-273) and wrapped by the deployment's sandbox policy before it runs,
so a hosting environment's privilege isolation is preserved. If the vendor binary
is not on `PATH`, declare its location in `[[secrets.placement]]` and the bundle
will fail the probe with a clear instruction.

### Tool classification and exposure

Every tool the MCP server exposes must be either in the manifest's `[tools.allow]`
list (personal tier) or have a corresponding `[config.mcp.tools.<name>]` entry that
sets its classification (enterprise/federal). Server-supplied annotations are
untrusted and ignored; classification comes only from the manifest.

```toml
[tools]
allow = ["SEARCH_DOCUMENTS", "READ_FILE"]   # personal: allowlist is enough

[config.mcp.tools.SEARCH_DOCUMENTS]
classification = "read_only"

[config.mcp.tools.READ_FILE]
classification = "read_only"
```

### Declaring secrets

Credentials the operator must supply are declared under `[[secrets]]`. Each secret
must declare a **placement** — where the credential goes when the operator connects.
For stdio transport, the placement is the environment variable the spawned binary
reads. For http transport, the placement is carried by the manifest for reference,
but http credentials are revealed directly into the header mapping (they do not
transit environment).

```toml
[[secrets]]
name = "api_key"
prompt = "Your vendor API key"
format = "api_token"
sensitive = true

[secrets.placement]
variable = "VENDOR_API_KEY"     # env var for stdio; declarative for http
```

### Contract test (CON-15)

Every MCP connector must ship a runnable contract test against a spec-conformant
server. The test runs offline and validates the connector's attachment behavior
without requiring a live backend.

```python
import pytest
from arcagent.extension.authoring import assert_connector_contract
from .sdk_server import MockMcpServer  # your bundle's mock server

@pytest.mark.asyncio
async def test_mcp_connector_honours_the_contract() -> None:
    """Offline contract test — no vendor backend needed."""
    # Your bundle builds an attachment from the manifest.
    # The mock server emulates the MCP wire without a real backend.
    adapter = build_mcp_attachment(...)
    await assert_connector_contract(adapter, connection_id="test-conn")
```

This test ensures tool classification, revision monotonicity, error typing, and
other invariants hold before your connector is shipped.

### Example: Composio

Composio is a first-party, vendor-hosted MCP server that fronts hundreds of
third-party integrations. It demonstrates all the patterns above:

- **Transport:** HTTP (vendor-hosted MCP endpoint)
- **Auth:** `x-api-key` header (custom auth scheme)
- **Operator-minted URL:** Each operator creates an MCP server in Composio and
  receives a `backend.composio.dev/v3/mcp/{SERVER_ID}?user_id=...` URL
- **Trust boundary:** `url_origin = "https://backend.composio.dev/"` — only
  Composio's host is accepted
- **Tools:** Read-only discovery verbs only
  (`SEARCH_TOOLS`, `LIST_TOOLKITS`, `GET_TOOL`, `LIST_TRIGGERS`); action verbs are
  not granted in the shipped bundle to enforce egress posture (D-580)

See `extensions/composio/extension.toml` and the Composio SKILL guide for setup
and resource documentation.
