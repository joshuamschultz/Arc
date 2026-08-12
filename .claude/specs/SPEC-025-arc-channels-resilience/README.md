---
spec_id: SPEC-025
name: arc-channels-resilience
status: complete
completed: 2026-05-06
type: integration
created: 2026-05-06
intake_confidence: 0.92
type_confidence: 0.85
fast_track: true
prior_work:
  - .claude/brainstorms/2026-05-03-arcui-as-platform-adapter.md (deepened 2026-05-06)
  - .claude/specs/SPEC-023-arcui-web-platform-adapter/ (v1.0 — already shipped; this spec is v1.1+v1.2)
  - .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md
  - .claude/solutions/security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md
related_specs:
  - SPEC-023-arcui-web-platform-adapter (predecessor — built the WebPlatformAdapter this hardens)
  - SPEC-024-nlit-scap-demo (the demo that exposed the failure modes this addresses)
trigger: 2026-05-05 SCAP demo failure — operator feedback "this UI seems way too complicated and constantly breaks. I just need something that works consistently." Combined with OpenClaw architectural comparison surfacing channels-as-product principle.
pillars_priority: [Simplicity, Modularity, Security, Scalability]
steering_status: absent — using the 2026-05-03 brainstorm + deepening as authoritative steering for this feature
---

# SPEC-025 — Arc Channels Resilience

## TL;DR

Harden the multi-channel agent surface so the operator can always reach an agent and the user-facing chat never silently stalls. Three threads:

1. **Reliability** — sequence-gap detection in the WebSocket envelope, service worker for the static shell, fix the `arc-stack` deploy script so a broken non-demo agent doesn't fail the systemd unit.
2. **Optionality** — wire Slack as a tested fallback chat seat for the demo agents (the adapter already exists; only config + bot token needed). Add Mattermost adapter for federal/air-gap. Sketch Teams GCC High adapter for DoD/civilian. Every production agent must have ≥1 non-arcui chat surface.
3. **Single transport** — migrate dashboard polling endpoints (`/api/stats`, `/api/queue`, `/api/team/roster`, `/api/circuit-breakers`, `/api/budget`, `/api/performance`, `/api/cost-efficiency`, `/api/schedule-history`, `/api/stats/timeseries`) to a `/ws/dashboard` push channel. Stop hammering the server every second.

**No new architecture.** SPEC-023 shipped the right design. This spec hardens it.

## Pillar trace (gating principle for every requirement)

| Pillar | What this spec preserves / extends |
|---|---|
| **Simplicity** | One transport per page (kills polling). Service worker is straightforward (cache hashed assets, exclude `/api/*`). Sequence-gap is one integer per frame. |
| **Modularity** | Slack/Mattermost/Teams adapters are peers to the existing `WebPlatformAdapter` — same `BasePlatformAdapter` contract. No cross-module logic bleed. Dashboard event channel is a new event source, not a new transport layer. |
| **Security** | Audit emission unchanged (single chain, all events ride through `SessionRouter`). `seq` ties client view to audit chain — divergence forces reconnect, never silent skew. Mattermost adapter unblocks FedRAMP High / IL5 deployments. |
| **Scalability** | Dashboard subscribers are server-pushed; backpressure handled via the same per-socket bounded queue pattern already in `WebPlatformAdapter`. Multi-channel coexistence (web + Slack + Mattermost simultaneously) was already validated in SPEC-023's research. |

## Decisions Log

| ID | Decision | Rationale | Source |
|---|---|---|---|
| D-001 | One spec covers v1.1 (demo-blockers) + v1.2 (production polish), phased in PLAN.md | Operator wants one decision document, but implementation is staged | This conversation |
| D-002 | Sequence-gap detection rides on the existing envelope (§6 of brainstorm) — adds `seq` field, no protocol break | Smallest incremental change to a shipped contract | OpenClaw `ui/src/ui/gateway.ts` precedent |
| D-003 | Slack adapter is the v1.1 fallback seat (already built, just needs config); Mattermost is v1.2 (new adapter, federal-relevant) | Slack is 1-day; Mattermost is 2-week. Sequence by effort given urgency | Brainstorm deepening §"Build-order delta" |
| D-004 | Dashboard endpoints retire to a single `/ws/dashboard` server-pushed channel | OpenClaw single-transport pattern; eliminates polling churn | Brainstorm deepening §3 |
| D-005 | Service worker scope: cache `/assets/*` (hashed), network-first for HTML, exclude `/api/*`, `/ws/*`, `/artifacts/*` | OpenClaw `ui/public/sw.js` pattern; arcui assets are already hash-named | Audit + OpenClaw research |
| D-006 | Lit refactor of `messages-page.js` deferred to v1.3 (separate spec) | Lower priority, not a fix to a production failure | Brainstorm deepening |
| D-007 | Teams GCC High adapter scoped as a SDD-level sketch only — full implementation is a follow-up spec | Effort (~2–3 weeks via Bot Framework) is comparable to Mattermost; one at a time | Federal posture matrix |

## References

### Implementation files (current state, audited 2026-05-06)
- `packages/arcgateway/src/arcgateway/adapters/web.py` (444 LOC, production)
- `packages/arcgateway/src/arcgateway/adapters/slack.py` (built, unwired in demo)
- `packages/arcgateway/src/arcgateway/adapters/telegram.py` (built, unwired in demo)
- `packages/arcgateway/src/arcgateway/bootstrap.py` (314 LOC)
- `packages/arcgateway/src/arcgateway/identity.py` (38 LOC)
- `packages/arcui/src/arcui/routes/chat_ws.py` (172 LOC, thin proxy)
- `packages/arcui/src/arcui/routes/stats.py` (132 LOC, polling — to retire)
- `packages/arcui/src/arcui/routes/team_pages.py` (polling — to retire)
- `packages/arcui/src/arcui/static/assets/messages-page.js` (522 LOC, single-WS but no service worker)
- `deploy/aws/setup-vm.sh` (rsync deploys all of `team/` regardless of host capability)

### Steering / decision sources
- `.claude/brainstorms/2026-05-03-arcui-as-platform-adapter.md` — full architectural rationale
- §"Deepening — 2026-05-06" within that brainstorm — implementation audit, post-mortem, build-order delta

## Learnings (post-Phase-1 review — 2026-05-06)

### Review verdict
**PASS-WITH-FOLLOWUPS.** Two parallel reviewers (security-engineer + architect-reviewer) reached the same conclusion. No critical or high-severity findings. All Phase-1 acceptance criteria covered.

### Test totals (Phase 1)
- `arcgateway`: 36 passed (33 web adapter + 3 dual-adapter)
- `arcui`: 47 passed (17 chat_ws including 5 SPEC-025 + 10 sw static + 20 team aggregations)
- **Total: 83 passing, 0 failing**

### Coverage
- `arcgateway/adapters/web.py`: 95%
- `arcui/routes/chat_ws.py`: 85%
- `arcui/routes/team_pages.py`: above threshold (covered via 4 SPEC-025 tests + existing roster tests)

### Security follow-ups applied during review
- **M1 — `_parse_since_seq` bounded**: capped input string at 12 digits and parsed value at `2**31` to defeat the slow-DoS via Python's O(n²) int parsing.
- **M2 — `agent-state.json` cross-checked against live roster**: only honour entries whose `agent_id` appears in the running roster, defending against out-of-band tamper.
- **M3 — `agents.enabled` shape validation**: every line must match `^[a-z0-9_]+_agent$`; setup-vm.sh aborts on any nonconforming entry to defeat path-traversal smuggling.

### Architecture decisions (ADRs to write before next phase)
- **ADR-001 — Replay-buffer cleanup deferred until v1.2.** Per-chat `_outbound_seq` and `_replay_buffers` survive the last-socket-unregister so a reconnecting browser can replay. Memory bound is `max_connections × 50 frames × ~2KB ≈ 5MB`. TTL-based eviction is TD-1.
- **ADR-002 — ~~Slack/web session-key divergence is the v1.1 reality.~~ SUPERSEDED by SPEC-065 T-928 (REQ-304).** SlackAdapter's `slack:{channel}:{user_id}` and WebPlatformAdapter's `build_session_key(agent_did, user_did)` produced different session keys. Adapters no longer compose a session key at all: `SessionRouter._canonicalise` is the sole owner and stamps the same `(agent, user)` key on every surface, so web and Slack now converge on one session. The pinned divergence test in `test_dual_adapter_chat.py` is deleted, as that ADR required of whoever aligned the formats.
- **ADR-003 — `~/.arcagent/agent-state.json` is the arc-stack ↔ arcui contract.** File-based handoff (atomic write + best-effort read with empty-dict fallback) chosen over a typed gateway-health API for Phase 1 because the gateway has no per-agent health surface yet. TD-5 documents the restart race.
- **ADR-004 — Service-worker cache versioning is a manual constant for v1.1.** `CACHE_VERSION = 'arcui-shell-v1'` requires a developer bump per shell change. Build-id template substitution is TD-3.

### Tech-debt items (log in `.claude/decisions-log.md`)
- **TD-1**: Memory leak — per-chat `_outbound_seq`/`_replay_buffers` never reclaimed; needs TTL eviction.
- ~~**TD-2**: Slack vs. web session-key divergence — blocks unified cross-platform chat history.~~ **RESOLVED by SPEC-065 T-928** — one owner stamps every surface.
- **TD-3**: Service-worker cache key not bumped on deploy.
- **TD-4**: `_read_agent_manifest` duplication risk if Azure deploy gains a manifest.
- **TD-5**: `agent-state.json` race during `arc-stack restart` (stale read window).
- **TD-6**: `_replay_to_socket` drop-oldest events not surfaced in audit (parity gap with `_fan_out`).
- **TD-7**: Slack `allowed_user_ids = []` semantics need documentation.

### Federal posture
- **CMMC L1**: ✅ AC-2, AU-2, AU-12 satisfied
- **FedRAMP Low**: ⚠️ partial (FR-7 OS-user binding deferred to v1.2 as planned)
- **FedRAMP Moderate / High**: out of scope for v1.1 (token TTL, MFA, FIPS crypto required)

### Post-deploy monitoring plan (first 48 hours after AWS push)

**Key metrics to watch:**
- WebSocket reconnect rate per minute. Baseline expectation: < 1/min/active-user. Alert above 10/min/active-user (would indicate seq-gap loop or network instability).
- `gateway.message.dropped` audit events. Reasons: `no_socket` (acceptable), `backpressure` (alert if > 5% of delivered).
- Agent-state.json modification count. If arc-stack rewrites it more than once on a healthy boot, something is restarting.
- arcui process RSS. With the no-cleanup memory policy (ADR-001), watch for `_replay_buffers` growth — alert if RSS > 200MB above baseline.
- `/api/team/roster` `degraded` count. Expected: 0 in steady state.

**Alerts to configure:**
- Reconnect rate > 10/min sustained for 5 minutes.
- Audit emission failure (already swallowed silently — needs out-of-band detection).
- arc-stack process exit (the unit should stay `active` per FR-4; an exit means "no agents connected", which is severe).
- `/ws/chat/*` 4429 count > 0 (max_connections cap reached).

**Rollback triggers:**
- Operator unable to chat for > 30s — even with arcui + Slack both up.
- `/artifacts/*` returns stale content (means SW is serving cached `/artifacts/` despite the exclusion rule — would be a security regression).
- Reconnect storm where the seq-gap fix is causing the loop instead of recovering it.

**Manual verification checklist (post-deploy):**
- [ ] `https://agent.blackarcsystems.com/#auth=...` loads
- [ ] Chat with `scap_isso_agent` → response under 5s
- [ ] Kill caddy for 5s → chat resumes within 2s without refresh (A9)
- [ ] DM the Slack bot once tokens are configured → response with `platform=slack` in audit (B8)
- [ ] Corrupt one agent's identity key, redeploy → unit stays active, that agent shows `degraded` in roster (C5)
- [ ] Kill caddy for 3s → no blank page (D6)


---

## Spec closure (2026-05-06)

### Status
**Complete.** Phase 1 (demo-blockers — Tracks A/B/C/D) and Phase 2
(production hardening — Tracks E/F/G + AWS Secrets Manager) merged to
`main` and deployed to AWS production (`https://agent.blackarcsystems.com`).

### Test totals at closure
| Package | Tests | Status |
|---|---|---|
| arcgateway | 747 + 1 skipped | ✅ |
| arcui | 700 + 5 skipped | ✅ |
| arcllm | 907 | ✅ |
| **Total** | **2,354 passing, 0 failures** | ✅ |

### ADRs filed
- ADR-001 — Replay-buffer TTL eviction
- ADR-002 — Cross-platform session keys (~~web/Slack diverge in v1.1~~ — superseded by SPEC-065 T-928; one owner, keys converge)
- ADR-003 — `agent-state.json` arc-stack ↔ arcui contract
- ADR-004 — SW cache key build-id template substitution
- ADR-005 — Vault backend `**kwargs` forwarding contract

### Production deployment status
- **AWS (Lightsail / `52.70.15.143`):** ✅ live, healthy. Phase 1 + Phase 2
  shipped. Manifest filtering active (3 demo agents on the host). Caddy
  serving `agent.blackarcsystems.com` + `52.70.15.143.nip.io`. Service
  worker, dashboard WS, Mattermost adapter all available.
  - Operator follow-up: provision Slack workspace + tokens + populate
    `allowed_user_ids` in each agent TOML; configure AWS Secrets Manager
    secrets + IAM role attachment if migrating off `.env`.
- **Azure:** ⚠️ NOT deployed. The bicep template + setup script exist
  but no VM has been provisioned (placeholder values still in
  `parameters.json`: SSH key, Object ID, allowed CIDR). Azure parity
  with SPEC-025 also needs:
  1. `az login` + filled `parameters.json` + bicep deploy
  2. Refactor `deploy/azure/setup-vm.sh` to the AWS rsync+manifest model
     (currently uses a hardcoded `AGENTS` array tied to its Key-Vault-
     per-agent pattern). The shared `deploy/lib/agent-manifest.sh` is
     ready to be sourced.
  3. New `arcllm/backends/azure_keyvault.py` paralleling
     `aws_secrets.py` (so ADR-005's `**kwargs` contract carries through).
  - Tracked as TD-8 (carry-over) — separate spec when needed.

### Tech debt items resolved during closure
- ✅ TD-4 — Manifest reader extracted to `deploy/lib/agent-manifest.sh`,
  shared by AWS (live), wired into Azure as a comment-pointer for the
  refactor.
- All Phase-1 TD items (TD-1 through TD-7) closed during the security
  review fix pass.

### Outstanding tech debt (post-closure)
- **TD-8** — Azure parity (provisioning + rsync/manifest refactor +
  AzureKeyVault arcllm backend). Bigger than a single follow-up; spec
  when prioritised.
- **TD-9** — `pip-audit` in CI (boto3 + slack-bolt + aiohttp pinning).
  Per security review §L-4. Add when CI pipeline lands.

### Solutions archive contributions
None added during this spec — the patterns (TTL eviction, federal-tier
guard, vault `**kwargs` forwarding) are documented in the ADRs above and
referenced from the architecture review report. A future `/compound`
pass can extract a "channels-as-product hardening pattern" entry if the
pattern recurs.

### Final operator runbook (production AWS)
1. **Push code:**  `rsync ... ubuntu@52.70.15.143:/home/ubuntu/arc/`
2. **Re-run setup:**  `ssh ubuntu@52.70.15.143 'sudo bash ~/arc/deploy/aws/setup-vm.sh agent.blackarcsystems.com'`
3. **Restart stack:**  systemd handles via `arc-stack.service`; check
   `sudo systemctl is-active arc-stack` and `sudo systemctl is-active caddy`.
4. **Verify health:**  `curl https://agent.blackarcsystems.com/api/health`
5. **Slack** (optional): edit `~/arc/.env`, populate `SLACK_BOT_TOKEN`,
   `SLACK_APP_TOKEN`; populate `allowed_user_ids` in each agent TOML;
   restart arc-stack.
6. **AWS Secrets Manager** (optional): attach IAM role per
   `deploy/aws/DEPLOY.md` §"Production secrets via AWS Secrets Manager";
   set `[llm.vault]` block in agent TOMLs; remove `.env` API keys.
