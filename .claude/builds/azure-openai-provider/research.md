# azure-openai-provider — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-232–D-251 (20 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Azure OpenAI Provider (SPEC-010)

**Date**: 2026-02-25
**Spec**: SPEC-010
**Feature**: Azure OpenAI Service adapter for ArcLLM (Azure AI Foundry / GCC)

#### Architecture

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Data Model

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### API Design

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Observability

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Audit & Compliance

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Security

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Integration

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Performance

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Extensibility

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Testing

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Deployment

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|

#### Summary

**20 decisions total**: 13 auto-applied (patterns + mandates), 7 user-decided.
**Key insight**: This is a thin adapter (~30 LOC) because Azure OpenAI uses OpenAI-compatible format. All meaningful differentiation is in URL construction and auth header.
**Risk**: The only dangerous regression is someone adding `?api-version=` to the URL — guarded by explicit test.

---

---
