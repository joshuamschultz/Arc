# connector-extensions — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-540–D-580 (41 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Connector Extensions — Build Decisions (2026-08-04)

**Phase**: build | **Status**: complete | **Total decisions**: 22 (16 user, 6 auto-applied)
**ID range**: D-540 to D-561
**Priority framework**: simplicity → modularity → security → scalability

#### Summary
Installable, signed connector extensions that let an agent reach Confluence, Jira, Gmail, Google Drive, Google Calendar, Outlook Mail, Outlook Calendar, OneDrive, Dropbox, and 1Password. Finishes MCP as a real dispatching transport, delivered as an optional arcagent module so an agent without it still runs. Grounded in research of how Hermes (connector = optional-skill folder + mcp_servers config block) and OpenClaw (separate plugin and mcp.servers registries, probe-before-save, OAuth creds in a state DB, per-server toolFilter globs, env safety filter) solve the same problem.

#### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|





#### Research Insights

**From Solutions Archive:**
- No hits — .claude/solutions/ contains only security-issues and nothing matches.

**Best Practices:**
- MCP 2026-07-28 is stateless: no initialize handshake, no Mcp-Session-Id. Send tools/list directly with _meta.io.modelcontextprotocol/protocolVersion populated; servers MUST implement server/discover. Core client ~150-400 LOC — building it under CON-7 is cheap. (modelcontextprotocol.io/specification/2026-07-28/changelog)
- Two-tier error handling is mandated: protocol errors (unknown tool, malformed request) return a JSON-RPC error object; tool execution errors return success with isError:true in result.content. Only the second kind should reach the LLM for self-correction — feeding protocol errors to the model wastes turns.
- Streamable HTTP is the transport for new work. HTTP+SSE has been deprecated since 2025-03-26 and is now eligible for removal. Clients MUST send Accept: application/json, text/event-stream and mirror MCP-Protocol-Version / Mcp-Method / Mcp-Name headers against the body or the server returns HeaderMismatch (-32020).
- stdio framing: newline-delimited JSON-RPC, no embedded newlines, UTF-8. stdout carries MCP messages ONLY; stderr is free-form and its presence must NOT be treated as an error. Shutdown escalates close-stdin, then SIGTERM, then SIGKILL.
- The spec says stdio implementations SHOULD NOT use the OAuth authorization spec — pull credentials from the environment instead. That is consistent with the broker pattern in the Security section.
- Arc's existing seam is `[tools.mcp_servers]` under ToolsConfig (core/config.py), not a module namespace. A `modules/mcp/` module (D-547) must reconcile with that existing config location rather than introduce a second one.
- The module contract is concrete and must be met exactly: a folder under `modules/` is only discovered if it ships BOTH `capabilities.py` and `_runtime.py` (core/module_discovery.py:40-52). Discovery and activation are deliberately separate, so discovered-but-disabled is a valid listable state and a config entry naming an absent folder never loads.
- `configure_module_runtimes` (core/agent_lifecycle.py:216-269) dispatches BY SIGNATURE — a module receives only the kwargs it names as parameters, from an offered set including `policy_pipeline`, `egress_proxy`, `human_gate`, `operator_signer`, `identity` and `tier`. The rationale at :242-247 is security: core names no module, so a module cannot harvest signing authority without explicitly asking for it. The MCP module should ask for the narrowest set it can do its job with.
- Module runtime state must live on a `contextvars.ContextVar`, never a module global. modules/scheduler/_runtime.py:1-19 documents why: a plain global is silently overwritten by whichever agent's asyncio task last called configure(). This is the already-resolved cross-agent bleed class.
- RISK, and it cuts against this whole feature: module scan roots are TRUSTED. `_UNTRUSTED_ROOTS` (capabilities/capability_loader.py:84-94) covers only workspace, global, and agent roots and their -skills variants; each enabled module is appended as a trusted scan root at agent_lifecycle.py:152-154. If connector bundles land inside a module directory they inherit that trust, which is the opposite of treating every upstream as hostile. Bundles should load through an untrusted root with the signature gate applied, not ride the module's trusted root.

**Edge Cases:**
- Legacy servers: a stateless client hitting a pre-2026-07-28 server gets a 400 with UnsupportedProtocolVersionError. Use server/discover as the legacy-detection probe and fall back to the old initialize handshake.
- Results now carry resultType: 'complete' or 'input_required'. The input_required round trip replaces server-initiated sampling/elicitation — the client retries the SAME call with inputResponses under a new JSON-RPC id. A client that ignores resultType will silently treat a half-finished call as done.
- Tool discovery at module load (D-548) must handle a server that is down at boot. With lazy spawn discovery happens on first use, so the registry needs a declared-then-reconciled path or the first call races registration.
- ttlMs and cacheScope now appear on list results. Honoring cacheScope: 'private' matters for multi-account instances — caching a private tool list across two Gmail instances would cross accounts.

**Performance:**
- Statelessness removes per-connection session state, so tool lists no longer vary per connection and can be cached across agents — relevant given per-agent bundle copies would otherwise duplicate discovery work.
- Lazy spawn costs roughly 800ms-1.2s on first call for a Node server. Budget it against the < 500ms cold-start target: the agent boots fast, the first connector call does not.

**References:**
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
- packages/arcagent/src/arcagent/core/config.py:152 (MCPServerEntry), :194 (ToolsConfig.mcp_servers)
- packages/arcagent/src/arcagent/tools/_transport.py:31 (ToolTransport.MCP)




#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- MCPServerEntry as it stands is stdio-only — command, args, env, timeout_seconds (core/config.py:152-158). Extending it for this feature needs: url + transport (hosted servers), package + version + sha256, tool allowlist, per-tool contract hash (rug-pull defense), account/instance binding, approval mode, and a tier floor.
- Every real upstream exposes a tool-filtering mechanism, and they differ: workspace-mcp uses --tools plus tiers core/extended/complete; ms-365-mcp-server uses --preset plus --enabled-tools regex plus --allowed-scopes plus --read-only; mcp-atlassian uses ENABLED_TOOLS or TOOLSETS plus READ_ONLY_MODE; agentmail-mcp uses --tools. The manifest needs a place to carry per-server launch flags, not just a name list — Arc's own allowlist is the enforcement layer, the upstream flag is defense in depth.
- Named instances (D-550) map onto how these servers actually work. Both workspace-mcp and ms-365-mcp-server run one process per account with its own token directory and OAuth client. That is the documented multi-account pattern, not a workaround.
- A tool allowlist ALREADY EXISTS at a different altitude: `ToolConfig.allow` / `ToolConfig.deny` (core/config.py:132-133), enforced by `ToolRegistry._policy_allows` (core/tool_registry.py:257-267), deny-wins. A per-server allowlist must COMPOSE with that rather than duplicate it, or two allowlists will disagree and the narrower one will be a surprise.
- `ModuleConfig` (core/module_config.py:14-21) sets `extra="forbid"`, so a typo'd key in `[modules.mcp.config]` raises instead of silently vanishing. The extension manifest model should do the same.
- Blueprint v2 has the pattern for a lower-trust manifest that must not be allowed to touch dangerous settings: `_DENIED_OVERLAY_PATHS` plus `_strip_denied` (arccli/blueprints.py:73-83, :445-458) silently drops `vault.backend`, `tools.process`, `tools.preamble`, `tools.policy.allowed_paths`, `identity.key_dir` and the custody paths, logging rather than rejecting. An extension manifest is exactly such a lower-trust document and needs the same denylist.
- Blueprint v2 also fails LOUD on an unknown reference: `_discover_prompt_overlays` (blueprints.py:362-386) raises when an overlay names a prompt not in the catalog, because "a typo'd override silently no-ops — the failure mode we fail loud on." A manifest naming a tool the server does not expose should fail the same way at probe time.
- Store per connected account: refresh token (encrypted), access-token expiry, last-successful-refresh timestamp, issuer, and audience. Expiry and last-refresh are what let a watcher refresh proactively rather than reactively on a 401.

**Edge Cases:**
- Rotating refresh tokens (Atlassian) are single-use. Two processes refreshing the same account concurrently means the AS accepts the first and invalidates the rest, and a late writer can persist a token the AS has already rejected — the account is then permanently broken until a human re-consents. Needs single-writer-per-account and atomic write-temp-then-rename.
- None of Google, Entra, Atlassian, or Dropbox documents a grace window for a just-rotated-out token (unlike Cognito's ~60s). Treat all four as strictly single-use on rotation.
- Per-agent bundle copies multiply the refresh-race surface: ten agents each holding their own copy of the same connector, if ever pointed at the same account, are ten independent refreshers of one rotating token.
- Google silently invalidates the oldest refresh token when a client+user pair exceeds 50 grants. A fleet that re-authorizes on every deploy will hit this.

**Performance:**
- Refresh proactively at 75-80% of access-token lifetime rather than lazily on 401. Dropbox access tokens last 4 hours, so roughly every 45 minutes to an hour. Lazy-on-401 under concurrency is exactly what triggers the rotation race.

**References:**
- packages/arcagent/src/arcagent/core/config.py:152-158
- https://developer.atlassian.com/cloud/oauth/getting-started/refresh-tokens/
- https://nango.dev/blog/concurrency-with-oauth-token-refreshes/
- https://github.com/modelcontextprotocol/typescript-sdk/issues/1760
- https://www.unipile.com/google-oauth-refresh-token/
- https://developers.dropbox.com/oauth-guide



#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- OpenClaw's verb set is worth copying nearly wholesale, because it covers the states a connector actually has: add (build and probe before saving), configure (partial update), tools (manage the allowlist), login / logout (credential lifecycle), probe and doctor --probe (prove it starts and lists tools), status (classify without connecting), reload (dispose cached runtimes), unset (remove). D-551 covers add and auth; the operational verbs are the gap.
- `arc ext doctor` earns its place: a static check plus an optional live probe is how an operator distinguishes 'misconfigured' from 'upstream is down' without reading logs.
- The install prompt should show what it is about to grant — probe returned N tools, allowlist admits M, tier denies K. The preview Josh selected already implies this; it is also the moment a rug-pull hash is first recorded.
- Josh's amendment stands as a testable contract: every arcui action must have a CLI equivalent. Build the CLI verbs first and let arcui call the same code path, or the two will drift.
- `connect_telegram()` (arcgateway/connect.py:27-56) validates the token against a regex BEFORE writing anything, so a malformed credential leaves no partial state. Probe-before-save should extend that principle, not replace it.
- The existing split is the right one to copy: prompting lives in the CLI (arccli/commands/gateway_connect.py uses `getpass.getpass` at :59 so the secret is never echoed), while the write logic lives in arcgateway because arcui needs it too (connect.py:10-11). A connector's guided setup belongs in the same shape — shared write path, surface-specific prompting.
- THREE-INSTANCES RULE FIRES: `_dump_toml`/`_emit_table`/`_scalar` at connect.py:97-130 is already a near-verbatim duplicate of blueprints.py:461-483. This feature needs a third TOML emitter, which is the moment to extract the shared one rather than copy it again.

**Edge Cases:**
- Probe-before-save cannot work unmodified for hosted servers with browser-only OAuth (Atlassian's official endpoint). Either the probe runs after auth, or the install flow forks by transport.
- A device-code bootstrap (Microsoft, Google) needs the operator to visit a URL on another device. The CLI must render that URL and block, and the same flow has to work over SSH to the DGX.
- `arc ext add` on an agent whose [modules.mcp] is absent should say so plainly rather than half-installing — D-545 ships the module off by default, so this is the common first-run error.

**Performance:**
- Probing on install costs one server spawn. Doing it at install rather than at every boot is what keeps the < 500ms cold start.

**References:**
- https://docs.openclaw.ai/cli/mcp
- packages/arcgateway/src/arcgateway/connect.py:34-74 (guided connect + _upsert_env, the existing prompt-and-store precedent)

#### Observability

_(no decisions in this category for this feature)_




#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- Full capture (D-552) is the right call for AU-3 but makes the audit store the highest-value target on the box. It needs its own access control, and the classification labels have to be real enough to drive it.
- The credential carve-out (D-553) has an existing mechanism: arcllm._pii carries a SECRETS_CATEGORY constant, a PiiDetector Protocol, a RegexPiiDetector with custom_patterns and entity toggles, and redact_text — 365 LOC already written and used by arcllm/modules/security.py.
- Redaction must run in the MCP module before the event reaches arctrust.audit.emit. arctrust is a leaf and importing arcllm from it would invert the dependency DAG.

**Edge Cases:**
- Full capture of mail bodies means the audit store now contains every email the fleet reads. Retention and access policy for that store is a separate decision this feature forces.
- Regex-based secret detection has false negatives. A credential that does not match a pattern will be captured in full — the carve-out should key off the tool's declared classification (a 1Password read is always sensitive) rather than relying only on pattern matching.
- Hosted connectors mean content also exists in the vendor's logs. Full local capture does not give complete custody for AgentMail, Atlassian's hosted endpoint, or Dropbox's remote MCP.

**Performance:**
- Running a regex detector over every connector payload adds per-call cost proportional to content size. Mail bodies are large; budget it or scope detection to declared-sensitive tools.

**References:**
- packages/arcllm/src/arcllm/_pii.py:16 (SECRETS_CATEGORY), :35 (PiiDetector Protocol), :274 (RegexPiiDetector), :352 (redact_text)
- packages/arcllm/src/arcllm/modules/security.py:12 (the existing consumer)






#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- RECOMMENDATION AGAINST D-556 AS WRITTEN: package pinning does not defend against tool poisoning. CVE-2025-54136 (CVSS 8.8) confirmed a server can serve a benign tools/list at approval time and swap in malicious tool descriptions later; a CSA lab benchmark across 45+ real servers measured >60% attack success (72.8% best case). The spec has no continuous re-verification. Add a per-tool contract hash over name + description + inputSchema, recorded at approval and re-checked on every tools/list, forcing re-approval on change. This is the ONLY defense that works for hosted servers where there is no package to pin.
- The spec requires treating tool `annotations` from untrusted servers as untrusted. Arc should ignore upstream annotations entirely and take classification from its own manifest.
- RECOMMENDATION AGAINST THE SUPPLY-CHAIN TIER SPLIT: sandboxing is nearly free at every tier. sandbox-exec on macOS and bubblewrap on Linux cost single-digit milliseconds with no daemon. Make sandboxing the unconditional default and let tier change only the SandboxPolicy object (bwrap → rootless Podman → Firecracker), keeping one launcher and one code path per the composability rule.
- THE BEST IDEA FROM THIS RESEARCH: the connector never receives the credential. A host-side broker holds it and attaches the Authorization header on egress; the sandboxed server gets a bare localhost URL and, on Linux, no network namespace at all. This is exactly how anthropic-experimental/sandbox-runtime works, and systemd LoadCredential= provides it natively on the DGX via a private tmpfs torn down at unit stop. It removes 'third-party code holds live credentials' as a risk rather than mitigating it.
- WORTH STEALING FROM OPENCLAW — the sentinel pattern. OpenClaw resolves secrets eagerly at activation into an in-memory runtime snapshot, then mints opaque process-local sentinels (`oc-sent-v1-...`) that stand in for the real value everywhere it would otherwise appear: logs, auth storage, SDK configuration. The real value is substituted immediately before the network request. It also never writes secrets to config and deliberately keeps no rollback backups containing historical plaintext. That is the same instinct as the broker, applied to the in-process path. (docs.openclaw.ai/gateway/secrets)
- WHERE OPENCLAW STOPS SHORT, and why the broker still wins: OpenClaw passes resolved secrets to plugins and MCP servers AS ENVIRONMENT VARIABLES. So the third-party server does receive the real token. The sentinel protects OpenClaw's own logs and config, not the spawned process. Arc's broker design is strictly stronger and the difference is the whole point of the supply-chain decision.
- OpenClaw's own 1Password answer is the `op` CLI with a service-account token file (`OP_SERVICE_ACCOUNT_TOKEN`) for headless, desktop-app IPC on user machines, and tmux-managed sessions for interactive signin. It prefers `op run` / `op inject` over writing secrets to disk. Notably it accepts the `op` CLI session-inheritance risk that this research flags — Arc should not, and should call the SDK directly instead.
- 1Password: use Service Accounts plus the official SDK, never MCP and never the `op` CLI. `op service-account create --vault Vault:read_items` scopes a token to exactly one vault, read-only, with no desktop app anywhere in the chain.
- Prefer a customer-supplied Entra app registration over Softeria's shared multi-tenant one. A tenant admin raised that calling certain Graph endpoints through the shared app can silently auto-provision a Power Platform Dataverse environment with no admin approval.
- THE TRIFECTA HOOK IS `capability_tags`. An MCP-backed tool rides the existing envelope by supplying nothing more than a RegisteredTool (tools/_transport.py:36-74) with an async execute, a JSON Schema input_schema, transport=ToolTransport.MCP, an honest classification, and capability_tags — because the tags are what feed `legs_for_call` and therefore the lethal-trifecta gate (tool_registry.py:452). Signing, policy, ledger, audit and timeout are all supplied by `_create_wrapped_execute` (tool_registry.py:384-600). D-557's default is therefore a tagging problem, not new gate machinery. agent_lifecycle.py:329-358 shows the exact shape of bridging a foreign registry into RegisteredTools.
- The dispatch order is fixed and an MCP tool inherits all of it: schema validation, ASI-03 identity-arg stripping, ToolCall built and SIGNED (no identity means unsigned means denied), PolicyPipeline under the session admission lock with first-DENY-wins, forbidden-composition routed to HumanGate OUTSIDE the lock, pre_tool bus veto, execute under asyncio.wait_for with a telemetry span, post_tool, then a tool.executed audit carrying actor_did and tier.
- Signing has an existing on-disk convention to reuse unchanged: a detached `.arcsig` sidecar holding an arctrust ArtifactSignature (capabilities/artifact_signing.py:21-60). A corrupt sidecar is treated as unsigned; verify_file fails closed.
- KEY INVARIANT for the supply-chain decision: requiring a signature implies PINNING A KEY. capability_loader.py:334-337 states it directly — without a pinned key, arctrust accepts any self-consistent signature, so an unpinned floor is no floor at all. The bundle must declare which key class it pins to: operator key (as blueprints and prompt overlays do, blueprints.py:303-318) or agent key (as capabilities and skills materialized into the agent root do, blueprints_materialize.py:257-269).
- The load-time trust gate re-verifies independently of any install-time check (capability_loader.py:323-376). Probe-and-verify at `arc ext add` does not remove the need to verify again at load, and any exception in the gate denies.
- Node's --permission flag is not a security boundary. Node's own documentation states it 'does not provide security guarantees in the presence of malicious code'; symlinks are unchecked, open file descriptors bypass it, and worker threads do not inherit it. Defense in depth only.

**Edge Cases:**
- Ubuntu 24.04+ AppArmor blocks unprivileged user-namespace creation, breaking bubblewrap with 'setting up uid map: Permission denied' unless the bwrap-userns-restrict profile is loaded. This will hit a fresh DGX Spark image and needs a one-time host-setup step.
- Firecracker needs /dev/kvm and is Linux-only — viable on the DGX (~125ms boot on aarch64), impossible on macOS. Any federal-tier design that assumes it must not become the only path.
- The `op` CLI leaks its authenticated session to every child process (disclosed Oct 2023, still live Feb 2026): a malicious npm post-install script running under an authenticated shell can dump every accessible vault.
- sandbox-exec has been deprecated on macOS since 10.11 and warns on every use, but Apple shipped no replacement for confining a non-App-Store headless process. Anthropic's own tooling still depends on it.
- Google service accounts with domain-wide delegation can impersonate any Workspace user. Google's own documentation advises against DWD where avoidable; least-privilege scoping is required if used.

**Performance:**
- bubblewrap and sandbox-exec add single-digit milliseconds. Rootless Podman costs roughly 1.2s cold start with about 2MB idle RSS against Docker's 54MB. Firecracker boots in about 125ms with under 5MiB overhead.
- The broker proxy adds one local hop per outbound call — negligible against network latency, and it is the only component holding credentials, so it is also the only one needing hardened memory handling.

**References:**
- https://github.com/anthropic-experimental/sandbox-runtime
- https://systemd.io/CREDENTIALS/
- https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-ai-agent-exfiltration-2/
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- https://1password.com/blog/service-accounts-sdks-agentic-ai
- https://www.1password.dev/environments/mcp-server
- https://manchicken.com/2026/02/17/1password-cli-adds-risk/
- https://github.com/Softeria/ms-365-mcp-server/discussions/233
- https://sebastion.dev/posts/ms-365-mcp-server-cwe601
- https://nodejs.org/docs/latest-v24.x/api/permissions.html
- https://etbe.coker.com.au/2024/04/24/ubuntu-24-04-bubblewrap/
- https://docs.openclaw.ai/gateway/sandboxing
- packages/arcagent/src/arcagent/tools/human_gate.py:120 (trifecta leg compositions, already wired)



#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- Per-service upstream verdicts. Google: taylorwilsdon/google_workspace_mcp — MIT, PyPI with SHA256, releases every 2-9 days, 120+ tools across 12 services, --tools scoping, headless refresh after one-time auth, personal and Workspace accounts. Microsoft: Softeria/ms-365-mcp-server — MIT, npm and Docker, v0.136.0, 300+ tools with --preset mail|calendar|files and --read-only, device-code auth that refreshes headlessly, MSA and org accounts. Dropbox: official Dash server (github.com/dropbox/mcp-server-dash, Apache-2.0, stdio, pinnable to a commit) for read and search, or Dropbox's hosted remote endpoint for full read/write with nothing to pin. Atlassian: no clean answer (see Extensibility). 1Password: no MCP at all, use the SDK.
- STRONGER GOOGLE OPTION FOUND — `openclaw/gogcli` (github.com/openclaw/gogcli): a Go CLI, not an MCP server. MIT, official OpenClaw org, 8.3k stars, distributed as brew tap, Docker images, and raw release binaries. Covers Gmail, Calendar, Drive, Docs, Sheets, Slides, Contacts, Tasks and ~a dozen more Google surfaces. Auth supports desktop OAuth clients, direct access tokens, Application Default Credentials, AND service accounts with domain-wide delegation — the genuinely unattended path. Tokens sit in the platform keyring by default with an encrypted file backend explicitly for headless Linux. Multi-account is native: one install routes among several accounts via `--account work`, matching the named-instances decision without running N processes. Output is JSON via `--json`, with prompts, progress and warnings kept on stderr.
- gogcli's safety controls are better than anything else surveyed and map directly onto Arc's own gates: `--readonly`, `--no-input`, `--enable-commands-exact` (a command allowlist), `--gmail-no-send` (a no-send policy, i.e. reads-free-outbound-denied enforced upstream), dry-run plans, and `--wrap-untrusted` for untrusted-content wrapping — an actual LLM01 prompt-injection mitigation shipped by the upstream. Safety profiles can be baked in at build time.
- gogcli would ride ToolTransport.PROCESS rather than MCP. That transport is equally dead today (only ToolTransport.NATIVE is ever constructed), so choosing it means finishing a second transport — but a single static Go binary is far easier to pin and hash than an npm or PyPI dependency tree, and it removes a Node or Python runtime from the trust surface entirely.
- Softeria must be pinned at v0.107.1 or later — earlier versions carry a CWE-601 open redirect in the HTTP-mode /authorize handler that forwarded a client-supplied redirect_uri to Entra unvalidated.
- sooperset/mcp-atlassian must be pinned at v0.22.0 or later, ideally 0.23.0, run with READ_ONLY_MODE unless writes are required, and never exposed on HTTP transport without confirming the auth-bypass fixes are present.
- Non-interactive auth exists only for Google (service account plus JWT bearer, optionally domain-wide delegation) and Microsoft (client-credentials with a certificate credential, which also gives the agent its own Graph identity rather than impersonating a person). Atlassian and Dropbox both require a one-time human consent.
- Device-code flow is a bootstrap mechanism, not an unattended one. Microsoft's own guidance is to use managed identity or a certificate-based service principal for unattended workloads.

**Edge Cases:**
- Atlassian's 90-day rotating refresh token dies from inactivity. A connector installed and then unused for a quarter is silently dead, and there is no non-interactive way back.
- Entra's Continuous Access Evaluation can revoke a valid token in near real time on password change, risk detection, or account disablement. Long token lifetime is not a guarantee of a working connection.
- Google's 7-day refresh cap applies while the OAuth consent screen sits in Testing mode. The consent screen must be moved to Production and verified, which is a human process with a lead time.
- AgentMail's npm and uvx packages are only stdio bridges to https://mcp.agentmail.to/mcp, and the tool schemas are fetched live from that hosted server. There is nothing to pin, and the tool surface can change after approval — the rug-pull case exactly.
- Distinguish transient from terminal auth failure: 5xx and network errors retry with backoff; invalid_grant, consent_required, and interaction_required are terminal and must stop retrying immediately, because retrying a burned rotating token only wastes the window.

**Performance:**
- Node-based servers (Softeria, agentmail-mcp) cost roughly 800ms-1.2s to spawn. Python servers via uvx are comparable. With lazy spawn (D-558) that is a per-connector first-call tax, not a boot tax.
- Atlassian's hosted endpoint rate-limits at 500 to 10,000 calls per hour depending on plan tier — a fleet sweeping tickets can hit that.

**References:**
- https://github.com/taylorwilsdon/google_workspace_mcp
- https://pypi.org/project/workspace-mcp/
- https://github.com/Softeria/ms-365-mcp-server
- https://github.com/dropbox/mcp-server-dash
- https://github.com/sooperset/mcp-atlassian
- https://github.com/agentmail-to/agentmail-mcp
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
- https://developers.google.com/identity/protocols/oauth2/service-account

#### Performance

_(no decisions in this category for this feature)_



#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- The vetting bar now has evidence behind it. Proposed written criteria: actively maintained (commits within 90 days, a release cadence); distributed through a registry that publishes integrity hashes (PyPI or npm) or pinnable to a commit SHA; no fetch-at-launch; auth that refreshes headlessly; a documented tool-filtering mechanism; a security-advisory history that has been read, not merely counted; and a recorded live conformance run.
- A read advisory history is worth more than an empty one. sooperset/mcp-atlassian's 35 advisories mean the code has been attacked and fixed; the official Atlassian endpoint has a clean record because it is a black box nobody outside Atlassian can audit.
- Josh already owns the skills half of two bundles: ~/.claude/skills/atlassian-jira (v1.2.0, complete Jira Cloud REST API v3, issue CRUD, JQL, boards, workflows) and ~/.claude/skills/atlassian-confluence (v1.0.0, page CRUD, spaces, templates, permissions). Both target ctgfederal.atlassian.net and can seed the skills/ directory of those bundles directly.
- Because those two skills already describe the REST API in full, a direct-API adapter for Jira and Confluence is a credible alternative to adopting an upstream with a critical-CVE history — the know-how is written, only the transport is missing.
- THE VETTED-UPSTREAM ALLOWLIST ALREADY HAS A PRECEDENT IN THIS REPO: `OFFICIAL_ADAPTERS: dict[str, str]` in arcgateway/adapters/registry.py:61-65 maps a plugin name to its expected distribution, and is the signed-allowlist control point — unofficial plugins load at personal and enterprise with a `gateway.adapter.unverified` audit event and are HARD-BLOCKED at federal (:240-248). A connector catalog should be the same shape: name to expected distribution plus pinned version and hash, warn below federal, refuse at federal.
- The plugin registry's other defensive mechanics are worth copying wholesale: `_VALID_NAME_RE = ^[a-z][a-z0-9_]{0,31}$` with `validate_adapter_name` (registry.py:57, :79-90) blocks `../evil` and `os.system` as connector names; `discover_plugins()` (:153-182) audits and skips a broken plugin rather than aborting discovery; missing credentials skip at personal and enterprise but raise at federal (:271-278); every load, skip and block emits a `gateway.adapter.*` audit event.
- The gateway proves config ownership can sit with the extension: `PlatformsSection` (arcgateway/config.py:119-144) declares only `web` explicitly and exposes every other dict-shaped key through a computed `extra` property, so a platform block reaches its plugin unvalidated by the core and the PLUGIN owns its schema (arcgateway_telegram/config.py:1-12). An extension manifest can work the same way, with the core validating only the fields it must enforce.
- The `platform = "telegram"` key (registry.py:223-232) lets ONE plugin back several distinctly-named config blocks. That is exactly the mechanism named instances (D-550) need — `gmail_work` and `gmail_personal` as two blocks over one bundle — and it already works.

**Edge Cases:**
- A THIRD CONNECTOR SHAPE emerged that the build session did not consider: a vetted CLI driven over ToolTransport.PROCESS, with JSON output. gogcli is the worked example, and OpenClaw ships it as a SKILL that wraps the binary rather than as a plugin — the skill carries the setup steps (`gog auth credentials <client_secret.json>`, then `gog auth add you@gmail.com --services gmail,calendar,drive`) exactly as Hermes' skills do. For services with a strong official-ish CLI, this may beat MCP on pinnability (one hashed binary), on trust surface (no Node/Python runtime), and on safety controls.
- Vendor-wide servers (Google, Microsoft) undercut the one-per-service packaging in D-546. The workable shape is one bundle per vendor with the manifest carrying per-instance launch flags, so a Drive-only instance and a Gmail-only instance come from the same bundle.
- Upstream tool-filter flags all differ (--tools, --preset, ENABLED_TOOLS, TOOLSETS, --enabled-tools, --allowed-scopes). The manifest must carry opaque launch arguments per instance; Arc's own allowlist stays the enforcement layer.
- Registry integrity guarantees vary. PyPI publishes SHA256 per artifact and npm publishes integrity hashes, but a Docker image needs digest pinning rather than tag pinning, and a git-source install pins to a commit SHA with no signature.
- Adopting an upstream is a recurring commitment, not a one-time check. A pinned version means we own the upgrade decision every time an advisory lands.

**Performance:**
- Shipping bundles in-repo means a connector update requires an Arc release. With ten connectors tracking upstream security releases, that cadence is the real cost of the decision.

**References:**
- /Users/joshschultz/.claude/skills/atlassian-jira/SKILL.md
- /Users/joshschultz/.claude/skills/atlassian-confluence/SKILL.md
- https://github.com/sooperset/mcp-atlassian/security
- https://support.atlassian.com/atlassian-rovo-mcp-server/docs/supported-tools/



#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- The fake MCP server should implement the 2026-07-28 stateless shape, including server/discover, resultType, and the two-tier error split, or CI proves a protocol we do not actually speak.
- Add a rug-pull regression test: approve a tool set, mutate the fake server's tool descriptions, call tools/list again, and assert Arc refuses and demands re-approval. That is the test for the highest-severity finding in this research.
- Add a refresh-race test: two concurrent refreshes against a fake AS that rotates single-use tokens, asserting exactly one refresh is in flight and the persisted token is the accepted one. Per the concurrency-test lesson, force interleaving with a barrier — an instant mock will pass while the bug survives.
- Assert the negative security paths explicitly: LD_*, DYLD_*, PYTHONSTARTUP and NODE_OPTIONS are stripped before spawn; a hash mismatch refuses and emits an audit event; a non-allowlisted tool is never registered; a wildcard allowlist is refused above personal tier.

**Edge Cases:**
- The live conformance run needs real credentials for ten services and cannot live in CI. It needs a defined home, a recorded evidence format, and an expiry — a conformance run from a year ago is not evidence.
- Hosted upstreams (Atlassian, Dropbox remote, AgentMail) cannot be pinned, so their conformance evidence goes stale silently whenever the vendor changes anything.
- Testing the broker means asserting a negative: the spawned process must never see the credential in its environment or on its filesystem. That is an inspection of the child's actual environment, not a mock.

**Performance:**
- A fake in-process MCP server avoids the roughly 1s spawn cost per test that a real Node server would add across a large suite.

**References:**
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- packages/arcagent/src/arcagent/tools/_transport.py (RegisteredTool.classification — the read_only/state_modifying contract to assert against)



#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- The auth prerequisite already exists but is on the wrong branch: commit 66326e54 'feat(auth): a person can sign in, so an approval can name one' lives on feat/one-button-deploy, not develop. It must land on develop before browser secret entry ships.
- Josh's amendment is a contract worth testing, not just stating: every arcui action needs a CLI equivalent. Building the CLI verbs first and having arcui call the same code path is what keeps them from drifting.
- The connections view should surface what the research says actually breaks: token expiry with a countdown, last successful refresh, and whether the tool-contract hash still matches. Health alone hides the two most likely failures.
- Approval prompts should name the instance and the recipient, not just the tool. 'sales wants to email new@stranger.com' is decidable; 'sales wants to call gmail.send' is not.

**Edge Cases:**
- A device-code bootstrap needs the operator to open a URL on another device. Both the CLI over SSH and the browser flow must render it.
- Atlassian connections silently die after 90 days of disuse and cannot be revived without a human. The UI must show impending expiry rather than reporting failure after the fact.
- A pending outbound approval with nobody watching blocks the agent. Notification routing and a timeout policy are needed — HumanGatePolicy.timeout_seconds already defaults to 300s (core/config.py).

**Performance:**
- The connections view reads from arcstore (D-541), so it costs no live probing. Health must come from recorded state, not a probe per page load.

**References:**
- git commit 66326e54 (arcui sign-in, on feat/one-button-deploy)
- packages/arcagent/src/arcagent/core/config.py (HumanGatePolicy.timeout_seconds = 300.0)

#### Open Questions
- Which specific upstream repo is adopted for each of the ten services, and what is the written vetting bar that lets one in? (research for /deepen)
- Which services genuinely have a maintained MCP server versus needing an adapter.py under D-546?
- If one upstream covers a whole vendor workspace (Drive + mail + calendar in one), can an operator install only part of it, or is the tool allowlist the only lever?
- What is the exact extension.toml schema, including the tier floor and the per-instance approval setting?
- How does OAuth token refresh work without paging a human each time, given the per-agent env file default in D-555?
- Which connector ships first as the proof that MCP dispatch is real?
- How do blueprints declare a connector dependency, given bundles are not blueprint-owned (D-559)?
- What sandbox mechanism backs the enterprise and federal path in D-556, and does it work on both macOS and the DGX?
- Does ADR-018 (no MCP) get formally reversed by a new ADR, or amended in place?

#### Research Insights

**From Solutions Archive:**
- No hits in .claude/solutions/.

**Best Practices:**
- RESOLVED — which upstream per service: Google → taylorwilsdon/google_workspace_mcp (PyPI, MIT, --tools scoping). Microsoft → Softeria/ms-365-mcp-server pinned >= v0.107.1 with our own Entra app (npm/Docker, --preset). Dropbox → dropbox/mcp-server-dash for read and search, hosted endpoint for write. 1Password → no MCP, Service Accounts plus official SDK, one vault, read-only. Atlassian → unresolved, see below.
- RESOLVED — which services need an adapter rather than MCP: 1Password, definitively.
- RESOLVED — vendor-wide upstreams exist for Google and Microsoft, so packaging is one bundle per vendor with per-instance launch flags, and the tool allowlist is the lever that separates mail from files.
- RESOLVED — headless refresh: Google service accounts and Entra client-credentials are genuinely unattended. Atlassian and Dropbox need one human consent, then scheduled refresh at 75-80% of token lifetime with single-writer discipline.
- RESOLVED — sandbox mechanism: sandbox-exec on macOS and bubblewrap on Linux as the free default, rootless Podman then Firecracker as stricter policy objects on the same launcher. Firecracker is Linux-only.
- STILL OPEN — Atlassian has no clean choice. Three paths: adopt sooperset pinned >= 0.22.0 and accept a project with a recent critical-CVE history; use Atlassian's hosted endpoint and accept that it cannot be pinned, is Cloud-only, and is not FedRAMP; or write a direct-API adapter using the Jira and Confluence REST skills Josh already has. This is a decision for a follow-up /build session, not a research finding.

**Edge Cases:**
- STILL OPEN, AND NEW — MCP versus CLI-over-PROCESS per connector. gogcli (Google) and the 1Password SDK both argue that the best upstream for a given service is often not an MCP server. Both PROCESS and MCP transports are dead in the registry today, so this is a question of which to finish first, or whether the bundle manifest should simply declare its transport and support both. This was not on the table during /build.
- STILL OPEN — the first connector to ship. Google is the strongest candidate: cleanest upstream, both of Josh's accounts exercise named instances, and it hits mail, files, and calendar in one integration.
- STILL OPEN — the extension.toml schema. Research now constrains it: transport, package plus version plus hash, opaque upstream launch flags, per-tool contract hashes, account binding, approval mode, tier floor.
- STILL OPEN — how blueprints declare a connector dependency.
- STILL OPEN — whether ADR-018 is reversed by a new ADR or amended. The stateless 2026-07-28 spec and a 150-400 LOC client are new facts that were not available when ADR-018 was written.

**Performance:**
- Ten connectors tracking upstream security releases is a standing maintenance cost that the in-repo distribution choice converts into Arc release cadence.

**References:**
- docs/architecture/decisions/ADR-018-no-mcp-no-migration-no-acp.md
- https://blog.modelcontextprotocol.io/posts/2026-07-28/

#### Related Solutions
- .claude/brainstorms/2026-08-04-connector-extensions.md — the vision this implements
- project_arc_mcp_gap — MCP verified unwired on develop: the enum is `ToolTransport` (not TransportKind) at arcagent/tools/_transport.py:27-33, and mcp_servers at core/config.py:194, with no dispatch code. Confirmed dead by grep: the only construction site anywhere in package source is `transport=ToolTransport.NATIVE` at agent_lifecycle.py:349 — MCP, HTTP and PROCESS have no client, no dispatch, no registration path, and the ToolRegistry docstring claiming 4 transports (tool_registry.py:3) is aspirational. ADR-018 excluded MCP deliberately
- project_blueprints_v2_mcp_spine — MCP is an unfinished transport, not a missing abstraction; blueprints must allowlist tools per server
- project_gateway_adapter_plugins — the closest existing shape to a connector extension
- project_mechanical_approval_subsystem (SPEC-035) — the approval path the trifecta decision routes through
- project_trifecta_context_resolved (SPEC-057) — the three-leg model the trifecta decision implements
- project_task_reliability_engine (SPEC-056) — retry, backoff, and circuit-breaker patterns reused in D-543
- feedback_producers_unwired_pattern — the failure mode D-560 is designed to catch
- External research: Hermes optional-skills/email/agentmail (connector as skill folder plus mcp_servers config block) and OpenClaw docs.openclaw.ai/cli/mcp (probe-before-save, OAuth creds in state DB, per-server toolFilter globs, stdio env safety filter)


---

---

## Connector Extensions — Follow-Up — Build Decisions (2026-08-04)

**Phase**: build | **Status**: complete | **Total decisions**: 20 (20 user, 0 auto-applied)
**ID range**: D-562 to D-580
**Priority framework**: simplicity → modularity → security → scalability

#### Summary
Five decisions taken after the research pass, resolving what /deepen surfaced: the Atlassian upstream, tool-contract poisoning, the sandbox tier split, where bundles load from, and the attachment-surface architecture. Google's upstream was settled in the same session as taylorwilsdon/google_workspace_mcp, chosen over openclaw/gogcli specifically because the flagship connector should prove the MCP spine rather than bypass it.

#### Auto-Applied (Compliance Mandates)
_(none)_





#### Data Model

_(no decisions in this category for this feature)_

#### API Design

_(no decisions in this category for this feature)_

#### Observability

_(no decisions in this category for this feature)_

#### Audit & Compliance

_(no decisions in this category for this feature)_







#### Performance

_(no decisions in this category for this feature)_

#### Extensibility & Lockdown

_(no decisions in this category for this feature)_

#### Testing

_(no decisions in this category for this feature)_

#### UI/UX

_(no decisions in this category for this feature)_














#### Open Questions
- Does a self-contained extension vendor its own dependencies (e.g. the 1Password SDK) so removing the extension removes them, or does it declare them for the host to install? Vendoring gives true removability and per-extension isolation; declaring is lighter but shares a dependency tree.
- What exactly is the attachment surface — the Protocol or ABC an MCP bundle, an API adapter, and a skill-only bundle all implement? This is the central interface of the whole feature and is not yet specified.
- Which connector ships first? Google is the strongest candidate: cleanest upstream, both existing accounts exercise named instances, and it covers mail, files, and calendar in one integration.
- The exact extension.toml schema, now constrained by: attachment pattern, package plus version plus hash, opaque upstream launch flags, per-tool contract hashes, account binding, approval mode, tier floor, and a denied-key list modeled on _DENIED_OVERLAY_PATHS.
- How do blueprints declare a connector dependency?
- Is ADR-018 reversed by a new ADR or amended in place? The stateless 2026-07-28 spec and a 150-400 LOC client are new facts it did not have.
- Where does the guided-setup TOML writer live, given _dump_toml is already duplicated between arcgateway/connect.py:97-130 and arccli/blueprints.py:461-483 — this is the third instance and the moment to extract.

#### Related Solutions
- The Connector Extensions build decisions and their Research Insights blocks, immediately above in this log
- .claude/brainstorms/2026-08-04-connector-extensions.md
- Google upstream settled in this session: taylorwilsdon/google_workspace_mcp, chosen over openclaw/gogcli — gogcli is a stronger artifact in isolation (one hashed Go binary, native multi-account routing, --readonly, --gmail-no-send, --enable-commands-exact, --wrap-untrusted) but it rides PROCESS rather than MCP, and its safety controls are enforced upstream rather than by Arc's per-tool-call authorization, which is the product
- arcgateway/adapters/registry.py — OFFICIAL_ADAPTERS is the existing in-repo precedent for a vetted-upstream allowlist that warns below federal and hard-blocks at federal
- capabilities/artifact_signing.py — the .arcsig sidecar convention an extension bundle reuses unchanged

---
