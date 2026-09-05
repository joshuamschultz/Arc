# PLAN — SPEC-077 Voice Channel

**Status:** PENDING · TDD (RED → GREEN → REFACTOR). Every task carries a `domain:` tag from
`{test, api, backend, ui, db, ai-workflow, ai-chain, auth, infra, mixed}` for `/implement` routing.
Task IDs are monotonic `T-NNN`; each references its SDD `COMP-` and PRD `REQ-` in the description.

Gates: line ≥80 %, branch ≥75 %, core-component ≥90 %, complexity ≤10, ruff 0, mypy 0.
Package home: `packages/arcgateway/src/arcgateway/adapters/voice/`; client in `packages/arccli`.

---

## Phase 1: Foundation

- [x] **T-001**: Contract test for the `VoiceEngine`/`STTEngine`/`TTSEngine` Protocols against a `FakeVoiceEngine`; architecture test that the gateway imports/starts with `adapters/voice/` absent. (COMP-003, REQ-004)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_engine_contract.py, packages/arcgateway/tests/architecture/test_voice_adapter_deletable.py
  - parallel: true
- [x] **T-002**: `VoiceEngine` Protocol + `FakeVoiceEngine` + null wiring so the package imports and the seam resolves. (COMP-003, REQ-004)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/engine/base.py, packages/arcgateway/src/arcgateway/adapters/voice/__init__.py
  - parallel: false
- [x] **T-003**: Test that `VoiceAdapter` satisfies `AdapterSpec` (payload→Part, Part→wire) and reply routes to voice origin only. (COMP-001, REQ-001, REQ-002)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_adapter_contract.py
  - parallel: true
- [x] **T-004**: `VoiceAdapter` skeleton implementing `AdapterSpec`; utterance(text Part)→dispatch, final-text→engine.speak; reply to voice origin only. (COMP-001, REQ-001, REQ-002)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/adapter.py, packages/arcgateway/src/arcgateway/adapters/voice/config.py
  - parallel: false
- [x] **T-005**: Test `TierGate` refuses load on federal, permits personal/enterprise. (COMP-014, REQ-016)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_tier_gate.py
  - parallel: true
- [x] **T-006**: `TierGate` config load-gate (refuse-load on federal, no runtime branch). (COMP-014, REQ-016)
  - domain: auth
  - files: packages/arcgateway/src/arcgateway/adapters/voice/tier.py
  - parallel: false

## Phase 2: Core (cascade engine, capture, transport)

- [x] **T-007**: Contract tests for `STTEngine`/`TTSEngine` (fake + one real each); `CascadeEngine` yields agent-authored text only (stubbed agent → exact spoken string). (COMP-003/004/005, REQ-005)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_cascade_engine.py
  - parallel: true
- [x] **T-008**: `CascadeEngine` = STT → agent dispatch → TTS; optional model deps inside the boundary; engine-down → typed degraded result. (COMP-003, REQ-004, REQ-005)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/engine/cascade.py
  - parallel: false
- [ ] **T-009**: `STTEngine` (Whisper/Voxtral adapter) behind the seam. (COMP-004, REQ-004)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/engine/stt.py
  - parallel: true
- [ ] **T-010**: `TTSEngine` (Olivia-cloned voice) behind the seam. (COMP-005, REQ-005)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/engine/tts.py
  - parallel: true
- [x] **T-011**: Test `ArtifactVerifier` refuses an unpinned/unverified model artifact. (COMP-017, REQ-022)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_artifact_verifier.py
  - parallel: true
- [x] **T-012**: `ArtifactVerifier` hook on engine load (sign + provenance + version-pin). (COMP-017, REQ-022)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/engine/verify.py
  - parallel: false
- [ ] **T-013**: Test `WakeGate`: no audio pre-wake; wake and PTT both emit "utterance start". (COMP-006, REQ-007, REQ-011)
  - domain: test
  - files: packages/arccli/tests/unit/voice/test_wake_gate.py
  - parallel: true
- [ ] **T-014**: `WakeGate` client: openWakeWord self-trained "hey Olivia" ONNX + Mac PTT; idle = no stream. (COMP-006, REQ-007, REQ-011)
  - domain: backend
  - files: packages/arccli/src/arccli/voice/wake.py
  - parallel: false
- [ ] **T-015**: Test `Endpointer` declares exactly one utterance for speech + ~0.7 s trailing silence. (COMP-008, REQ-010)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_endpointer.py
  - parallel: true
- [ ] **T-016**: `Endpointer`: Silero VAD over AEC'd audio + WebRTC-VAD gate + tuned hangover. (COMP-008, REQ-010)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/endpoint.py
  - parallel: false
- [ ] **T-017**: Transport tests: unauthenticated/fingerprint-mismatch peer refused; barge-in fires while playback active. (COMP-007, REQ-008, REQ-009)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_transport.py
  - parallel: true
- [ ] **T-018**: `MediaTransport`: WebRTC (Opus 20 ms/VOIP/24 kHz, AEC), DataChannel control, DTLS-SRTP + mTLS, pinned fingerprints, host-only candidates. (COMP-007, REQ-008, REQ-009)
  - domain: infra
  - files: packages/arcgateway/src/arcgateway/adapters/voice/transport.py, packages/arccli/src/arccli/voice/transport_client.py
  - parallel: false
- [ ] **T-019**: `arc voice start` thin client wiring WakeGate + MediaTransport; reconnect watcher. (COMP-002, REQ-003)
  - domain: backend
  - files: packages/arccli/src/arccli/voice/client.py, packages/arccli/src/arccli/voice/commands.py
  - parallel: false

## Phase 3: Integration (UX modules, identity, audit)

- [x] **T-020**: Test `OutputContract` collapses a long markdown reply to short markup-free speech even with the prompt overlay bypassed. (COMP-009, REQ-012)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_output_contract.py
  - parallel: true
- [x] **T-021**: `OutputContract`: code post-processor (word cap, strip markdown/links, list→summary, spoken/detail split) + signed arcprompt overlay. (COMP-009, REQ-012)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/ux/output_contract.py
  - parallel: true
- [x] **T-022**: Test `ProgressManager`: ack < 1 s on a stubbed 90 s turn, ≥1 heartbeat, hard-timeout failure on a hung turn. (COMP-010, REQ-013)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_progress_manager.py
  - parallel: true
- [x] **T-023**: `ProgressManager`: ack → periodic earcon/heartbeat → hard timeout; wraps dispatch; generic fillers only. (COMP-010, REQ-013)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/ux/progress.py
  - parallel: true
- [x] **T-024**: Test `ConfirmationGate`: irreversible action needs explicit "yes" against param read-back; ambiguity/silence aborts; reversible implicit. (COMP-011, REQ-014, REQ-018)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_confirmation_gate.py
  - parallel: true
- [x] **T-025**: `ConfirmationGate`: risk-tiered gate between intent and tool exec; read-back of actual params; bounded yes/no; ambiguity→abort; sits with PolicyPipeline. (COMP-011, REQ-014, REQ-018)
  - domain: auth
  - files: packages/arcgateway/src/arcgateway/adapters/voice/ux/confirmation.py
  - parallel: true
- [x] **T-026**: Test `AudioFeedback`/`Interruption`: earcons on state events; backchannel does not cut off; noise during read-back does not confirm. (COMP-012, REQ-015)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_audio_feedback.py
  - parallel: true
- [x] **T-027**: `AudioFeedback` + `Interruption`: fixed earcon set; barge-in classifier; raised bar during read-back; content redirect via `enter_held_messages`. (COMP-012, REQ-015)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/ux/audio_feedback.py
  - parallel: true
- [ ] **T-028**: Test `VoicePairing`: personal refuses unpaired mic; enterprise unapproved mic raises a pending grant (no action). (COMP-013, REQ-017)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_pairing.py
  - parallel: true
- [ ] **T-029**: `VoicePairing`: personal operator-signed pairing; enterprise TOFU via mechanical approval (SPEC-035 grant store). (COMP-013, REQ-017)
  - domain: auth
  - files: packages/arcgateway/src/arcgateway/adapters/voice/pairing.py
  - parallel: false
- [ ] **T-030**: Test `VoiceAudit`: full turn emits ordered events in JsonlSink + SignedChainSink with caller_did + mic id; no raw audio at rest; transcript ciphertext. (COMP-015, REQ-019, REQ-020)
  - domain: test
  - files: packages/arcgateway/tests/unit/adapters/voice/test_audit.py
  - parallel: true
- [ ] **T-031**: `VoiceAudit` wiring: `arctrust.audit.emit` per op; transcript encryption at rest; audio never in session/prompt; opt-in short-TTL encrypted audio. (COMP-015, REQ-019, REQ-020)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/audit.py
  - parallel: false

## Phase 4: Polish (observability, UI, abuse battery, e2e)

- [ ] **T-032**: `VoiceTelemetry`: OTEL span/turn with `first_ack`/`first_token`/`done` + wake/false-wake/barge-in/latency metrics. (COMP-016, REQ-021)
  - domain: backend
  - files: packages/arcgateway/src/arcgateway/adapters/voice/telemetry.py
  - parallel: true
- [ ] **T-033**: arcui connection card shows voice pairing status per agent. (COMP-018, REQ-023)
  - domain: ui
  - files: packages/arcui/web/src/components/connections/VoiceChannelCard.tsx
  - parallel: true
- [ ] **T-034**: Journey test: real granted pairing → spoken utterance → agent run → spoken reply, faking only the audio wire and the LLM wire. (REQ-001, REQ-002, REQ-005, REQ-014)
  - domain: test
  - files: packages/arcgateway/tests/e2e/test_voice_journey.py
  - parallel: false
- [ ] **T-035**: Abuse battery: forged/replayed pairing, replayed audio frame, wake spoof, federal load, spoken injection, stolen handle, barge-in flood, TOFU race, confirmation-by-noise. (REQ-009, REQ-014, REQ-016, REQ-017, REQ-018)
  - domain: test
  - files: tests/security/test_voice_abuse.py, scripts/run_adversarial_tests.py
  - parallel: false
- [ ] **T-036**: Deploy: signed cascade model bundles on the DGX; `arc voice start` systemd (DBUS_SESSION_BUS_ADDRESS=/dev/null); macOS mic TCC + Linux PortAudio/ALSA docs; rollback = disable `[adapters.voice]`. (REQ-022; D-756/757/763)
  - domain: infra
  - files: docs/runbooks/voice-channel-deploy.md, packages/arccli/src/arccli/voice/service.py
  - parallel: false

---

## Dependencies (order)

Phase 1 (T-001..006) → Phase 2 (T-007..019) → Phase 3 (T-020..031) → Phase 4 (T-032..036).
Within a phase each `domain:test` task precedes its implementation task (RED before GREEN).
T-034/T-035 depend on Phases 1–3; T-033 depends on T-029.

## Traceability check

Every PRD REQ-001..023 maps to ≥1 task; every SDD COMP-001..018 has an implementing task.
