# arcflow — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-501–D-539 (39 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## ArcFlow — Named Signed Workflows — Build Decisions (2026-08-01)

**Phase**: build | **Status**: complete | **Total decisions**: 37 (32 user, 5 auto-applied)
**ID range**: D-501 to D-537
**Priority framework**: simplicity → modularity → security → scalability

#### Summary
Named, semi-permanent, signed workflow.toml graphs (nodes: agent/tool/script/router/gate) authored conversationally/IDE/arcui, executed on the existing task-DAG substrate by a deterministic fleet runner owned by arcteam. Design draft: .claude/specs/SPEC-061-arcflow/DESIGN.md.

#### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|

















































#### Open Questions
- arcui editor scope in Phase 3: full graph editing or node-property editing over a rendered graph first?
- Event triggers (message/webhook/task-created): which lands first after v1?
- Does the group channel bind per workflow or per run (thread-per-run inside one channel is the likely answer)?

#### Related Solutions
_(none)_

---

### Deepening Summary

**Applies to:** Connector Extensions (D-540 to D-561), the feature section immediately below.
**Deepened on:** 2026-08-04
**Sections enhanced:** 10
**Solutions referenced:** 0
**Skills matched:** atlassian-jira (user skill — complete Jira Cloud REST API, already written), atlassian-confluence (user skill — complete Confluence Cloud REST API, already written), python-patterns, review-rulebooks

#### Key Findings
- MCP spec 2026-07-28 removed sessions and the initialize handshake entirely — the protocol is now stateless. A correct from-scratch Python/httpx client is ~150-400 LOC core (+300-500 LOC if we implement OAuth 2.1 ourselves), which makes honoring CON-7 (no vendor SDKs) cheap rather than costly.
- PACKAGE PINNING DOES NOT STOP TOOL POISONING. CVE-2025-54136 (CVSS 8.8): a server can serve a clean tools/list at approval time and swap in malicious tool descriptions later. A CSA lab study across 45+ real servers measured >60% attack success. The spec has NO continuous re-verification mechanism. The version+sha256 pin protects the artifact but not the tool contract, and does nothing for hosted servers (Atlassian, Dropbox, AgentMail) where there is no artifact.
- The connector process never needs the credential. A host-side broker holds it and attaches it on egress; the sandboxed server only ever sees a localhost URL. This is how Anthropic's sandbox-runtime works and systemd LoadCredential= does it natively on the DGX. It dissolves the central risk in the brainstorm ('third-party code holds live credentials').
- Sandboxing at personal tier is nearly free — sandbox-exec on macOS, bubblewrap on Linux, single-digit ms, no daemon. The supply-chain decision reserved sandboxing for enterprise+; the evidence says make it the unconditional default with tier changing only the SandboxPolicy object, not the code path.
- 1Password cannot be reached over MCP on a headless box: the official MCP server manages CI environment bundles only, never returns secret values, and requires the desktop app to authorize every call. The correct path is Service Accounts + official SDK with a token scoped to one vault, read-only — i.e. the adapter.py escape hatch in D-546 is load-bearing, not hypothetical.
- Neither Atlassian option clears the vetted-upstream bar cleanly. The official server is a hosted endpoint (unpinnable, Cloud-only, browser OAuth, no allowlist, and Atlassian states it does not meet FedRAMP or HIPAA). The community server (sooperset/mcp-atlassian) has every mechanic we want but carries 35 advisories including CVE-2026-27825 (critical, arbitrary file write to RCE) and a critical auth bypass, all patched in one hardening release on 2026-07-10.
- Google is the cleanest service: taylorwilsdon/google_workspace_mcp covers Drive+Gmail+Calendar in one MIT server on PyPI with SHA256 hashes, releases every 2-9 days, `--tools gmail drive calendar` scoping, and headless refresh after one-time auth. Two Google accounts run as two instances, matching D-550 exactly.
- Microsoft is answered by Softeria/ms-365-mcp-server: mail+calendar+OneDrive in one server, npm+Docker, device-code auth that refreshes headlessly, and --preset mail|calendar|files for surface separation. Microsoft's own Agent 365 servers are closed-source and tenant-gated, failing the bar outright.
- Module scan roots are TRUSTED in the capability loader, so a connector bundle placed inside a module directory would inherit trust it must not have — bundles need an untrusted root plus the signature gate.
- The vetted-upstream allowlist already has a working precedent in-repo: OFFICIAL_ADAPTERS in arcgateway/adapters/registry.py warns below federal and hard-blocks at federal.
- Arc already has the mechanism for the trifecta-approval default. RegisteredTool.classification is Literal['read_only','state_modifying'] and defaults fail-closed to state_modifying (arcagent/tools/_transport.py). 'Reads free, outbound gated' is that field, not new machinery. The trifecta legs (private_data / external_comms / untrusted_input) are already wired in tools/human_gate.py and tools/_egress.py.
- MCPServerEntry today is stdio-only: command, args, env, timeout_seconds (core/config.py:152-158). It has no url/transport, no pinned version or hash, no tool allowlist, no account binding, and no approval mode — every field the extension-shape, multi-account, supply-chain and trifecta-approval decisions require is missing.

#### New Risks Discovered
- Rug-pull / tool poisoning has no mitigation in the current decisions. Mitigation: hash the approved tool name + description + inputSchema per server and force re-approval when the hash changes; treat tool `annotations` from untrusted servers as untrusted per spec.
- Rotating-refresh-token write-back race. Atlassian rotates single-use refresh tokens; two concurrent refreshes invalidate each other and a late writer can persist an already-rejected token, permanently breaking the connection. Same class as the scheduler-store-poison bug. Needs single-writer-per-account discipline plus atomic write-temp-then-rename.
- Atlassian offers no service-account or client-credentials path. Every Jira/Confluence connection needs a human through the 3LO consent screen once, and dies if unused for 90 days. The fleet cannot self-heal that.
- Google's 7-day refresh-token cap applies while the OAuth consent screen is in Testing mode, plus a 50-token-per-client+user cap where the 51st grant silently invalidates the oldest, plus 6-month inactivity revocation.
- Ubuntu 24.04+ blocks unprivileged user namespaces via AppArmor, breaking bubblewrap with 'setting up uid map: Permission denied'. This will bite on a fresh DGX Spark image and needs a one-time host-setup step.
- The `op` CLI leaks its authenticated session to every child process (disclosed Oct 2023, still live Feb 2026). The 1Password adapter must call the SDK directly and never shell out to `op`.
- arcui login exists only on feat/one-button-deploy (commit 66326e54), not on develop. D-561 puts secret entry in the browser, so that auth work must land on develop before this ships.
- Ecosystem baseline is bad: an OX Security scan (April 2026) found 43% of public MCP servers with command injection and 82% with path traversal. Cursor shipped two RCEs (CVE-2026-50548/50549) from MCP-response prompt injection. Any upstream we adopt is presumed hostile.


---

---
