# SPEC-077 — Voice Channel ("Hey Olivia")

Hands-free desk voice for Arc: say "hey Olivia," speak a request, hear a short answer. A
first-party gateway adapter bound to one agent; a thin local client owns mic/speaker/wake; the
voice engine is a cascade (STT → Arc agent → TTS) behind a swappable seam. Personal + enterprise
only; federal forbidden.

## Status

| Doc | Status |
|-----|--------|
| PRD | DRAFT (fast-track) — 23 requirements, EARS, pillar-tagged |
| SDD | DRAFT — 18 components, REQ→COMP traceability, 10-threat mitigation map |
| PLAN | IN PROGRESS — 22/36 done: pure-logic core + real STT/TTS (DGX-verified); transport/wake/pairing/audit/UI/deploy pending |
| README | this file |

## Provenance (workflow trail)

- Vision: `.claude/brainstorms/2026-09-04-voice-channel.md`
- Design decisions: `.claude/builds/voice-channel/decisions.md` → decisions **D-727..D-768**
  (deepened by 3 research agents; the PersonaPlex→cascade pivot is R-1/D-761)
- Global log: `.claude/decisions-log.md` (Voice Channel sections)
- Memory: `project_voice_channel_personaplex`

## The four decisions Josh made

1. V1 win = ask → agentic run → short spoken answer (Telegram parity, succinct + confirm-first).
2. Audience = personal + enterprise from day one; federal forbidden.
3. Mac = thin client, DGX runs the engine.
4. Voice engine = **cascade now, PersonaPlex optional/deferred** (after research showed
   PersonaPlex cannot voice Arc's text natively).

## Key reuse (not reinvented)

- Adapter contract = SPEC-065 `AdapterSpec`; pairing = SPEC-035 mechanical approval;
  audit = `arctrust.audit.emit`; barge-in redirect = `enter_held_messages` steering.

## Compliance

Regime `fedramp/nist` is declared but this channel is **federal-forbidden**. Binding controls:
Four Pillars (identity/sign/authorize/audit), encrypt-at-rest, PII redaction. SDD §5 maps 10
OWASP LLM/ASI threats to mitigations (tech.md mandate satisfied).

## Next

- `/implement SPEC-077` — execute the plan on a feature branch (TDD, phase approvals).
- `/validate SPEC-077` — 3 Cs quality check before implementing (optional).

## Learnings (filled during /implement)

### Phase 1 — Foundation (COMPLETE, 2026-09-04)

Implemented directly (6 tightly-coupled tasks in one new package, no parallelism
benefit) with strict RED→GREEN. Evidence: RED showed `No module named
'arcgateway.adapters.voice'`; GREEN = 58 passed / 1 skipped (voice + the full
adapter suite); ruff clean; `mypy --strict` clean on 5 source files.

Two spec refinements the real code forced (better than the SDD guessed):

1. **Tier gate is not an invented component.** `COMP-014 TierGate` = the voice
   `AdapterSpec.build()` raising `AdapterUnavailableError` at the federal tier,
   **plus** voice staying out of `registry.OFFICIAL_ADAPTERS` (the registry already
   blocks non-official adapters at federal). Defense in depth via the existing
   mechanism, not a new gate. SDD/PRD REQ-016 satisfied this way.
2. **The adapter is a folder exporting `PLATFORM = AdapterSpec(name, requires,
   supports, build)`**, not a free-standing class. `connect/disconnect/to_parts/send`
   is the whole surface; the gateway keeps audit/session/pairing/splitting.

**Guard for later phases:** `tests/adapters/test_adapter_contract_surface.py`
auto-parametrizes over every discovered adapter and greps each voice `.py` file for
forbidden markers. Later voice code must NOT use the literal identifiers
`session_key=`, `max_bytes`/`MAX_BYTES`, `MediaStore`, `"inbox"`, `build_session_key`,
`PairingStore`/`pairing_store`, `split_message`, or raw `open(...,'wb')`/`.write_bytes`.
In particular **T-029 VoicePairing** must name its store something other than
`pairing_store` (it delegates to the gateway's pairing boundary anyway).

Files added: `adapters/voice/{__init__,adapter,config}.py`,
`adapters/voice/engine/{__init__,base}.py`; tests under
`tests/unit/adapters/voice/` + `tests/architecture/test_voice_adapter_deletable.py`.

### Pure-logic core — COMPLETE (2026-09-04)

Done and verified (TDD, RED→GREEN, ruff + mypy --strict clean, 93 passed / 1 skipped
incl. the adapter contract-surface suite parametrized over voice):

- **T-007/008** CascadeEngine — STT+TTS behind the seam; agent dispatch stays the
  adapter/gateway's, so `speak` only voices given text (thin-face). Design note: the
  SDD's "STT → agent → TTS" was corrected — the engine must NOT hold the agent or it
  re-implements a gateway responsibility.
- **T-020/021** OutputContract — code-enforced ear-friendly replies.
- **T-024/025** ConfirmationGate — the load-bearing guard (explicit-yes / fail-closed).
- **T-022/023** ProgressManager — never-silent pacing + hard timeout.
- **T-011/012** ArtifactVerifier — fail-closed pin/digest/signature.
- **T-026/027** AudioFeedback + Interruption — earcons + barge-in classifier.
- **T-015/016** Endpointer — utterance segmentation over an injected VAD.

### Real models — DONE + DGX-verified (2026-09-04)

- **T-009/010** WhisperSTT (faster-whisper) + PiperTTS (Piper) impls of the seam,
  verified end-to-end on `spark-0290` (GB10, aarch64): real Piper→WAV→whisper
  round-trip through the actual classes returns the phrase exactly. `[voice]`
  optional extra declares the stack; models are per-box (never vendored). Mac dev
  parity: libs installed, no models. Deploy guide:
  `docs/runbooks/voice-channel-deploy.md`. Design note: the artifact verifier is
  wired but accepts a self-signed local model at personal/enterprise; cryptographic
  arctrust/Sigstore signing of model artifacts is the deferred integration.

### Remaining — transport / client / integration (still pending)

- **T-013/014** WakeGate (openWakeWord ONNX) + Mac PTT — needs the model + a mic.
- **T-017/018/019** WebRTC transport (aiortc, DTLS-SRTP + mTLS) + `arc voice start` client.
- **T-028/029** VoicePairing — wire to the SPEC-035 grant store (note: must NOT name its
  store `pairing_store` — contract-surface guard).
- **T-030/031** VoiceAudit — wire to `arctrust.audit.emit` + transcript encryption.
- **T-032** VoiceTelemetry (OTEL). **T-033** arcui card. **T-034** journey e2e.
  **T-035** abuse battery. **T-036** deploy (systemd, signed bundles).

Next: resume on the DGX with `arcgateway[voice]` extras installed.
