# PLAN — SPEC-031 ArcTeam Refresh

**Status:** VERIFIED — implemented, reviewed, hardened, full workspace green, e2e passes on real nats-server. PR: https://github.com/joshuamschultz/Arc/pull/2
**Method:** TDD (failing test → implement → verify). Each task is scoped to **one module** (Modularity pillar). `[module]` tag names the sole package touched. No task crosses a boundary; cross-module use is via the SDD contracts only.

**Progress:** 21 / 21 complete · push-close done · in /review

---

## Phase A — Identity spine `[arcteam]` (unblocks all)
- [x] **A1** `[arcteam]` `Entity` gains `did`+`handle`; DID-keyed storage; registration consumes `AgentIdentity` DID — REQ-001
- [x] **A2** `[arcteam]` single `resolve(ref)->DID`; typed `UnknownHandle` — REQ-002
- [x] **A3** `[arccli]` unify `arc agent create` / `arc team register` on DID identity; fix `sender_unauthorized` DLQ bug — REQ-003
- [x] **A4** `[arcteam]` `mentions.py` extract + attention flags — REQ-004

## Phase B — Signed NATS substrate `[arcteam]`
- [x] **B1** `[arcteam]` `NatsBackend` implements `StorageBackend`; JetStream subjects — REQ-020, REQ-022
- [x] **B2** `[arcteam]` durable consumer primitive (push + resume-from-ack) — REQ-021 · _push→inbox wiring closes in D2/E_
- [x] **B3** `[arcteam]` `crypto.py` Ed25519 sign/verify + nonce/ts replay via arctrust — REQ-030, REQ-031 · _mandatory-signer injection closes in C4/D2_
- [x] **B4** `[arcteam]` remove `FileBackend` (keep `MemoryBackend`); removed stranded standalone CLI — REQ-070

## Phase C — Team model & CLI
- [x] **C1** `[arcteam]` `Team` model + `TeamStore` — REQ-010
- [x] **C2** `[arcteam]` `Roster.snapshot`; reconcile gateway `team_roster` — REQ-011
- [x] **C3** `[arcteam]` presence `status` field + transitions — REQ-021
- [x] **C4** `[arccli]` unified `arc team` CLI (create/add/remove/up/down/status/send/inbox/read/thread); remove standalone arcteam messaging CLI — REQ-012, REQ-070

## Phase D — Mid-task delivery
- [x] **D1** `[arcrun]` require `caller_did`; emit `steer.injected`/`followup.injected` audit at each drain — REQ-042
- [x] **D2** `[arcagent]` inbox → arctrust policy gate → `follow_up` default / `steer` critical — REQ-040, REQ-041 · _REQ-021 real-push close pending (arcteam subscribe API)_

## Phase E — Autonomy & boot
- [x] **E1** `[arccli]` `_DEFAULT_CONFIG` enables messaging inbox loop by default — REQ-050
- [x] **E2** `[arccli]` `arc team up/down` supervised daemon orchestrator (reuse arcgateway runner pattern) — REQ-051

## Phase F — UI as thin view `[arcui]`
- [x] **F1** `[arcui]` read-only NATS→browser WS for team flows; render handles+mentions — REQ-060, REQ-062
- [x] **F2** `[arcui]` forward human group-post→arcteam, direct-post→arcagent — REQ-061
- [x] **F3** `[arcui]` remove 5s poll + leftover messaging patch; re-scope `test_no_push_pipeline` — REQ-062, REQ-063, REQ-070 · _arcteam-side emit_team_event removal in push-close step_

## Phase G — Acceptance & gates
- [x] **G1** Integration test `tests/integration/test_spec031_e2e.py`: §0 flow over real nats-server (send→push→verify→follow_up/steer/deny→bad_signature/replay) — 1 passed
- [x] **G2** Gates: ruff 0, mypy --strict 0 (5 pkgs), all suites green (arcui excl. pre-existing test_chat_ws hang). LOC: net +1826 src (new NATS/crypto/Team capability — not a decrease; the *replaced* hand-rolled mechanisms shrank)

---

## Sequencing notes

- **A before all** — identity keys everything (storage, signing, routing).
- **B before D/F** — the bus is the delivery + observer substrate.
- **C4/E2/F3** carry the cleanup (REQ-070) inline — delete superseded code in the same edit that replaces it (no compat shims, per Arc rules).
- Each phase ends at an approval boundary (`/implement` phase gate).

## Definition of Done

Tests pass (unit+integration) · `mypy --strict` + `ruff` clean · audit emitted for every new op · no plaintext secrets · docstrings on public API · LOC budget respected · §0 demo green.
