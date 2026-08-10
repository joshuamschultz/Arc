# multi-agent-ui-architecture — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-001–D-581 (37 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Multi-Agent UI Architecture — Build Decisions (2026-03-03)

**Phase**: build | **Status**: complete | **Total decisions**: 24 (15 user, 9 auto-applied)
**Priority framework**: simplicity > security > scalability > compliance
**Brainstorm**: inline conversation — covered current architecture analysis, 4 approach options, user decisions on topology/transport/layer-monitoring/control-plane
**Prior build**: arcui-llm-telemetry (2026-03-01) — 27 decisions covering initial single-agent UI

#### Summary

ArcUI evolves from an embedded single-agent dashboard to a standalone multi-agent control plane. UI runs as its own process (`arc ui start`), agents connect via WebSocket (`/api/agent/connect`), push events from 4 layers (llm, run, agent, team), and receive control commands on the same connection. UIReporter module in arcagent bridges all internal events to the UI. Three token types (viewer, operator, agent) with server-side identity binding. Per-agent aggregators + global rollup. Full control plane via REST proxy to agent WebSocket. Clean break from embedded mode.

#### Architecture Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Data Model Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### API Design Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Observability Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Audit & Compliance (all auto-applied)

| # | Decision | Choice | Mandate |
|---|----------|--------|---------|

#### Security Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Integration Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Performance Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Extensibility Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Testing Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Deployment Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### UI/UX Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

---

---
