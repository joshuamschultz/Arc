# SPEC-005: CDP Browser Module

| Field | Value |
|-------|-------|
| **ID** | SPEC-005 |
| **Feature** | CDP Browser Module |
| **Type** | Integration |
| **Status** | PENDING |
| **Confidence** | 95% |
| **Route** | Fast-track |
| **Package** | `arcagent` |
| **Created** | 2026-02-16 |

## Prior Work

- **Brainstorm**: `.claude/brainstorms/2026-02-16-cdp-browser-module.md`
- **Build Decisions**: `.claude/decisions-log.md` (15 decisions + research insights, Feature: CDP Browser Module)

## Key Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | CDP client library | cdp-use (browser-use's typed CDP layer) |
| 2 | Module structure | Tool-per-file |
| 3 | Tool API surface | Fine-grained individual tools |
| 4 | Element selection | Accessibility-first, CSS fallback |
| 5 | Page state format | Accessibility snapshot with ref IDs |
| 6 | CDP connection | Launch + connect |
| 7 | Tool registration | Auto-register on module load |
| 8 | URL security | Dual-mode allowlist/denylist |
| 9 | JS execution | Enabled by default, toggle in config |
| 10 | Credentials | Configurable persistence, ephemeral default |
| 11 | Timeouts | Per-tool defaults, ArcRun enforces |
| 12 | Screenshots | PNG base64 inline |
| 13 | Testing | Mock CDP WebSocket for unit, real Chrome for integration |
| 14 | Config | Flat under [modules.browser] |
| 15 | Events | Full action events |

## Learnings

(Captured during implementation)
