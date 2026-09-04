# PLAN — SPEC-077 Voice Channel

**Status:** PENDING · TDD (RED → GREEN → REFACTOR). Every task carries a `domain:` tag from
`{test, api, backend, ui, db, ai-workflow, ai-chain, auth, infra, mixed}` for `/implement` routing.
Task IDs are monotonic `T-NNN`. Each references its SDD `COMP-` and PRD `REQ-`.

Gates: line ≥80 %, branch ≥75 %, core-component ≥90 %, complexity ≤10, ruff 0, mypy 0.

---

## Phase 1 — Foundation (seams that must exist before anything else)

- **T-001** `[domain:test]` Contract test for the `VoiceEngine` Protocol against a `FakeVoiceEngine`; architecture test that the gateway starts with `adapters/voice/` physically absent. → COMP-003, REQ-004
- **T-002** `[domain:backend]` `VoiceEngine` Protocol + `FakeVoiceEngine` + null-object wiring so the adapter package imports and the seam resolves. → COMP-003, REQ-004
- **T-003** `[domain:test]` Test that `VoiceAdapter` satisfies `AdapterSpec` (payload→Part, Part→wire) and that deleting the folder leaves imports/startup green. → COMP-001, REQ-001
- **T-004** `[domain:backend]` `VoiceAdapter` skeleton implementing `AdapterSpec`; utterance(text Part)→dispatch, final-text→engine.speak; reply routed to voice origin only. → COMP-001, REQ-001, REQ-002
- **T-005** `[domain:test]` Test `TierGate` refuses load on federal and permits personal/enterprise. → COMP-014, REQ-016
- **T-006** `[domain:auth]` `TierGate` config load-gate (refuse-load on federal, no runtime branch). → COMP-014, REQ-016

## Phase 2 — Core (cascade engine + capture + transport)

- **T-007** `[domain:test]` Contract tests for `STTEngine` and `TTSEngine` (fake + one real each); test `CascadeEngine` yields agent-authored text only (stubbed agent → exact spoken string). → COMP-003/004/005, REQ-005
- **T-008** `[domain:backend]` `CascadeEngine` = STTEngine → agent dispatch → TTSEngine; optional model deps imported inside the engine boundary; engine-down → typed degraded result. → COMP-003, REQ-004, REQ-005
- **T-009** `[domain:backend]` `STTEngine` (Whisper/Voxtral adapter) behind the seam. → COMP-004, REQ-004
- **T-010** `[domain:backend]` `TTSEngine` (Olivia-cloned voice) behind the seam. → COMP-005, REQ-005
- **T-011** `[domain:test]` Test `ArtifactVerifier` refuses an unpinned/unverified model artifact. → COMP-017, REQ-022
- **T-012** `[domain:backend]` `ArtifactVerifier` hook on engine load (sign + provenance + version-pin). → COMP-017, REQ-022
- **T-013** `[domain:test]` Test `WakeGate`: no audio streamed pre-wake; wake and PTT both emit "utterance start". → COMP-006, REQ-007, REQ-011
- **T-014** `[domain:backend]` `WakeGate` client: openWakeWord (self-trained "hey Olivia" ONNX) + Mac PTT; idle = no stream. → COMP-006, REQ-007, REQ-011
- **T-015** `[domain:test]` Test `Endpointer` declares exactly one utterance for speech + ~0.7 s trailing silence. → COMP-008, REQ-010
- **T-016** `[domain:backend]` `Endpointer`: Silero VAD over AEC'd audio + WebRTC-VAD gate + tuned hangover. → COMP-008, REQ-010
- **T-017** `[domain:test]` Transport tests: unauthenticated/fingerprint-mismatch peer refused; barge-in fires while playback active (AEC). → COMP-007, REQ-008, REQ-009
- **T-018** `[domain:infra]` `MediaTransport`: WebRTC (Opus 20 ms/VOIP/24 kHz, AEC), DataChannel control, DTLS-SRTP + mTLS, pinned fingerprints, host-only candidates. → COMP-007, REQ-008, REQ-009
- **T-019** `[domain:backend]` `arc voice start` thin client wiring WakeGate + MediaTransport; reconnect watcher mirroring `_reconnect.py`. → COMP-002, REQ-003

## Phase 3 — Integration (UX modules + identity + audit)

- **T-020** `[domain:test]` Test `OutputContract` collapses a long markdown reply to a short markup-free spoken form even with the prompt overlay bypassed. → COMP-009, REQ-012
- **T-021** `[domain:backend]` `OutputContract`: code post-processor (word cap, strip markdown/links, list→summary, spoken/detail split) + signed arcprompt style overlay. → COMP-009, REQ-012
- **T-022** `[domain:test]` Test `ProgressManager`: ack < 1 s on a stubbed 90 s turn, ≥1 heartbeat, hard-timeout failure on a hung turn. → COMP-010, REQ-013
- **T-023** `[domain:backend]` `ProgressManager`: ack → periodic earcon/heartbeat → hard timeout; wraps dispatch; generic fillers only. → COMP-010, REQ-013
- **T-024** `[domain:test]` Test `ConfirmationGate`: irreversible action needs explicit "yes" against a param read-back; ambiguity/silence aborts; reversible uses implicit. → COMP-011, REQ-014, REQ-018
- **T-025** `[domain:auth]` `ConfirmationGate`: risk-tiered gate between intent and tool exec; read-back of actual params; bounded yes/no; ambiguity→abort; sits with PolicyPipeline. → COMP-011, REQ-014, REQ-018
- **T-026** `[domain:test]` Test `AudioFeedback`/`Interruption`: earcons on state events; backchannel ("mm-hmm") does not cut off; noise during read-back does not confirm. → COMP-012, REQ-015
- **T-027** `[domain:backend]` `AudioFeedback` + `Interruption`: fixed earcon set; barge-in classifier; raised bar during read-back; content redirect via `enter_held_messages`. → COMP-012, REQ-015
- **T-028** `[domain:test]` Test `VoicePairing`: personal refuses unpaired mic; enterprise unapproved mic raises a pending grant (no action). → COMP-013, REQ-017
- **T-029** `[domain:auth]` `VoicePairing`: personal operator-signed pairing; enterprise TOFU via mechanical approval (SPEC-035 grant store). → COMP-013, REQ-017
- **T-030** `[domain:test]` Test `VoiceAudit`: full turn emits ordered events in JsonlSink + SignedChainSink with caller_did + mic id; no raw audio at rest; transcript is ciphertext. → COMP-015, REQ-019, REQ-020
- **T-031** `[domain:backend]` `VoiceAudit` wiring: `arctrust.audit.emit` per op; transcript encryption at rest; audio never in session/prompt; opt-in short-TTL encrypted audio. → COMP-015, REQ-019, REQ-020

## Phase 4 — Polish (observability, UI, abuse battery, e2e)

- **T-032** `[domain:backend]` `VoiceTelemetry`: OTEL span/turn with `first_ack`/`first_token`/`done` + wake/false-wake/barge-in/latency metrics. → COMP-016, REQ-021
- **T-033** `[domain:ui]` arcui connection card shows voice pairing status per agent. → COMP-018, REQ-023
- **T-034** `[domain:test]` One journey test: real granted pairing → spoken utterance → agent run → spoken reply, faking only the audio wire and the LLM wire. → REQ-001..002, REQ-005, REQ-014
- **T-035** `[domain:test]` Abuse battery into `scripts/run_adversarial_tests.py`: forged/replayed pairing, replayed audio frame, wake-word spoof from a recording, federal-tier load attempt, spoken injection, stolen WS/DTLS handle, barge-in flood, TOFU race, confirmation-by-noise. → REQ-009, REQ-014, REQ-016, REQ-017, REQ-018
- **T-036** `[domain:infra]` Deploy: PersonaPlex-free cascade model bundles signed on the DGX; `arc voice start` as systemd (DBUS_SESSION_BUS_ADDRESS=/dev/null); macOS mic TCC + Linux PortAudio/ALSA docs; rollback = disable `[adapters.voice]`. → REQ-022; D-756/757/763

---

## Dependencies (order)

Phase 1 (T-001..006) → Phase 2 (T-007..019) → Phase 3 (T-020..031) → Phase 4 (T-032..036).
Within a phase, each `[domain:test]` task precedes its implementation task (RED before GREEN).
T-034/T-035 depend on Phases 1–3 complete. T-033 depends on T-029 (pairing status).

## Traceability check

Every PRD REQ-001..023 maps to ≥1 task above; every SDD COMP-001..018 has an implementing task.
