# SPEC-065 — Gateway Messaging + Media

| | |
|---|---|
| **Status** | Specified — not started |
| **Branch** | `feat/gateway-messaging-media` |
| **Type** | integration |
| **Workflow** | standard (phase approvals) |
| **Decisions** | D-668 – D-681 in [`.claude/decisions-log.md`](../../decisions-log.md) |
| **Requirements** | REQ-296 – REQ-318 (23, all Must) |
| **Components** | COMP-001 – COMP-014 |
| **Tasks** | T-923 – T-948 (26) across 4 phases |

## Documents

| File | State |
|---|---|
| [PRD.md](PRD.md) | Validated |
| [SDD.md](SDD.md) | Validated — every REQ traced to a component |
| [PLAN.md](PLAN.md) | Validated — every COMP has a task |

## Why this exists

Three defects, each verified against the tree rather than reported:

1. **A photo produces no run at all.** The Telegram adapter registers `MessageHandler(filters.TEXT, …)`, so a photo never reaches a handler — and `InboundEvent.message` is a bare `str`, so there is nowhere for media to travel even once it does.
2. **Every message starts a fresh run.** `deliver_message` is published only to the module bus for teammates; every human surface calls `agent.run()` instead, which always begins a new turn. The session was never rotating — it only looked that way.
3. **The arcui inbox is silently empty.** `ensure_nats_server` exists and works but has exactly one caller, `arccli/commands/_serve.py`. Any other launch path leaves the broker unstarted and `_connect_backend` returning `None`.

Plus one structural complaint: each platform is a separate package reimplementing the same security properties, so the fourth adapter is the fourth chance to forget a size cap.

## Phases

| Phase | Tasks | Ships |
|---|---|---|
| 1 · Foundation | T-923 – T-928 | The parts envelope and the media store everything writes through |
| 2 · Core | T-929 – T-936 | Delivery joins the live turn; history keeps refs; replay names files |
| 3 · Integration | T-937 – T-946 | Adapters in-tree, broker guaranteed, teammate parity |
| 4 · Polish | T-947 – T-948 | Trust boundary and the pairing collapse |

Phase 1 opens with **T-923 (red)** — the contract suite fails its inbound-image case against today's text-only adapters. That failure *is* defect 1, reproduced before anything is fixed.

## Operator constraints (REQ-315 – REQ-318)

Only add, simplify, condense, re-route. Specifically:

- Session history keeps being written to the workspace and keeps being listable and replayable. The gateway FIFO being deleted is transient inbound buffering and never held a line of it.
- A module left with no caller is deleted in the same change, not abandoned.
- Session replay names a media file; it does not serve its bytes to the browser.

## Open questions

- **Media retention is unspecified.** This writes artefacts into the workspace and gives them no lifetime.
- The 100-message flood cap disappears with T-932; reinstate it beside the agent's per-session lock only if flooding is observed.
- T-940 deletes the three adapter distributions outright — confirm nothing outside this repo installs them.

## Learnings

_(appended at phase boundaries during `/implement`)_
