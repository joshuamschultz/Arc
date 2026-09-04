# SDD — SPEC-077 Voice Channel

**Status:** DRAFT (fast-track) · Traces every `PRD REQ-NNN` to a component.
**Design source:** `.claude/builds/voice-channel/decisions.md` (D-727..D-768, deepened).

---

## 1. Architecture

```
  ┌─ desk (Mac or DGX) ─────────────┐        ┌─ DGX (engine host) ───────────────┐      ┌─ agent host ──┐
  │  arccli: `arc voice start`      │        │  arcgateway/adapters/voice/       │      │  arcagent     │
  │   ┌───────────────┐             │  WebRTC│   ┌─────────────────────────────┐ │ Part │  (arcrun loop,│
  │   │ WakeGate       │  mic ─────▶│  Opus  │   │ VoiceEngine (cascade)       │ │─────▶│  tools/memory,│
  │   │ (openWakeWord) │  spkr ◀────│  AEC   │   │  STTEngine → [agent] → TTS  │ │◀─────│  policy/audit)│
  │   │ + PTT (Mac)    │            │  DTLS  │   └─────────────────────────────┘ │ text └───────────────┘
  │   └───────────────┘            │  mTLS  │   VoiceAdapter (AdapterSpec)      │
  └────────────────────────────────┘        │   + 4 UX modules + pairing + audit│
                                             └───────────────────────────────────┘
```

Dependency direction (respects `.claude/rules/core.md`): `arccli` (client) → `arcgateway`
(adapter) → `arcagent` via the arcrun facade. `arctrust` (identity/sign/authz/audit) is a leaf
used by the adapter. No new upward edges; `arcagent` does not learn about voice.

## 2. Module boundaries

- **Client boundary** (`arccli`): audio hardware, wake word, PTT, WebRTC peer. Emits/receives
  only Opus media + control on the DataChannel. Knows nothing of the agent.
- **Adapter boundary** (`arcgateway/adapters/voice/`): the WebRTC peer's server side + the
  `VoiceEngine` + the four UX modules. Implements `AdapterSpec` only; the gateway still owns
  session identity, pairing custody, audit fan-out, and message splitting (REQ-310 in `base.py`).
- **Engine boundary** (`VoiceEngine` seam): STT/TTS/PersonaPlex model deps imported **inside**
  this boundary only; never leak into contract types. Engine-down → typed degraded result.
- **Agent boundary** (unchanged): receives a text Part, returns final text. No voice knowledge.

## 3. Components

| ID | Component | Home | Responsibility | Satisfies |
|----|-----------|------|----------------|-----------|
| **COMP-001** | `VoiceAdapter` | `arcgateway/adapters/voice/adapter.py` | `AdapterSpec` impl; owns the server WebRTC session; utterance→Part; final-text→speak; reply stays on origin | REQ-001, REQ-002 |
| **COMP-002** | `voice` CLI | `arccli` | `arc connect voice`, `arc voice start` (thin client) | REQ-003, REQ-011 |
| **COMP-003** | `VoiceEngine` seam + `CascadeEngine` | `adapters/voice/engine/` | typed Protocol; default STT→agent→TTS; Fake for tests; PersonaPlex slot | REQ-004, REQ-005, REQ-006 |
| **COMP-004** | `STTEngine` (Whisper/Voxtral) | `.../engine/stt.py` | audio → transcript; optional dep inside boundary | REQ-004, REQ-022 |
| **COMP-005** | `TTSEngine` (Olivia-cloned) | `.../engine/tts.py` | text → audio in Olivia's voice | REQ-005, REQ-022 |
| **COMP-006** | `WakeGate` | client (`arccli`) | openWakeWord + PTT; idle = no stream | REQ-007, REQ-011, REQ-022 |
| **COMP-007** | `MediaTransport` | client + adapter | WebRTC (Opus/AEC), DataChannel control, DTLS-SRTP + mTLS, host-only candidates | REQ-008, REQ-009 |
| **COMP-008** | `Endpointer` | adapter | Silero VAD + WebRTC-VAD gate + hangover → utterance start/end | REQ-010 |
| **COMP-009** | `OutputContract` | `adapters/voice/ux/output_contract.py` | code post-processor (cap/strip/collapse/split) + signed arcprompt overlay | REQ-012 |
| **COMP-010** | `ProgressManager` | `.../ux/progress.py` | ack → heartbeats → hard timeout; wraps the dispatch | REQ-013 |
| **COMP-011** | `ConfirmationGate` | `.../ux/confirmation.py` | risk-tiered read-back + bounded yes/no; ambiguity→abort | REQ-014, REQ-018 |
| **COMP-012** | `AudioFeedback` + `Interruption` | `.../ux/audio_feedback.py` | earcon set on state events; barge-in classifier; raised bar during read-back | REQ-015 |
| **COMP-013** | `VoicePairing` | adapter + `arctrust` grant store | personal signed pairing; enterprise TOFU via mechanical approval | REQ-017 |
| **COMP-014** | `TierGate` | adapter load path | refuse-load on federal (config, not branch) | REQ-016 |
| **COMP-015** | `VoiceAudit` wiring | adapter → `arctrust.audit.emit` | per-op events w/ caller_did + mic id; transcript encryption; no-audio-at-rest | REQ-019, REQ-020 |
| **COMP-016** | `VoiceTelemetry` | adapter → `core/telemetry.py` | OTEL span + latency marks + metrics | REQ-021 |
| **COMP-017** | `ArtifactVerifier` hook | engine load | sign/provenance/version-pin on models | REQ-022 |
| **COMP-018** | arcui connection card | `arcui` | pairing status per agent (optional) | REQ-023 |

## 4. Traceability (REQ → COMP)

REQ-001→001; 002→001; 003→002; 004→003/004; 005→003/005; 006→003; 007→006; 008→007;
009→007; 010→008; 011→002/006; 012→009; 013→010; 014→011; 015→012; 016→014; 017→013;
018→011/013; 019→015; 020→015; 021→016; 022→004/005/017; 023→018.

## 5. Threat → mitigation map (tech.md mandate: ≥1 per spec)

| Threat | Vector on this feature | Mitigation (component) |
|--------|------------------------|------------------------|
| **LLM01** Prompt injection | spoken "ignore instructions / do X" | content stays out of control plane; PolicyPipeline + ConfirmationGate (COMP-011), audit (COMP-015) |
| **LLM02/LLM07** Disclosure / prompt leak | audio or transcript leaking | no audio at rest, transcripts encrypted, audio never in prompt/session (COMP-015); OutputContract strips before speaking (COMP-009) |
| **LLM03/ASI04** Supply chain | poisoned STT/TTS/wake/PersonaPlex model | signed + provenance + version-pin (COMP-017) |
| **LLM05** Improper output handling | reading raw/injected markup aloud | code OutputContract filter (COMP-009) |
| **LLM06/ASI02** Excessive agency / tool misuse | voice fires a destructive tool | ConfirmationGate read-back + explicit yes (COMP-011) |
| **LLM10** Unbounded consumption | hung turn, infinite "thinking" | ProgressManager hard timeout (COMP-010) |
| **ASI03** Identity/privilege abuse | unknown mic acting | VoicePairing + caller_did on every event (COMP-013, COMP-015) |
| **ASI06** Memory poisoning | a second brain writing memory | thin-face: only the Arc agent authors/writes (COMP-003, REQ-005) |
| **ASI07** Insecure inter-node comms | LAN sniff/replay of audio | WebRTC DTLS-SRTP + mTLS + pinned fingerprints, host-only (COMP-007) |
| **ASI09** Human-agent trust | accidental/faked confirmation | explicit bounded read-back; raised barge-in bar during it (COMP-011, COMP-012) |

## 6. Key design decisions (from /build + /deepen)

- **Cascade over PersonaPlex** (D-761): thin-face by construction, stateless scale, self-hosted.
- **WebRTC over raw WS** (D-762): AEC is what makes barge-in over open speakers real.
- **Output contract in code, not only prompt** (D-764): unenforceable/injection-bypassable in a
  prompt; the code filter is also a security boundary.
- **Confirmation gate is the load-bearing guard** (D-766): human is commit authority.
- **Reuse, not reinvent:** pairing = mechanical approval (SPEC-035); audit = `arctrust.audit.emit`;
  barge-in content redirect = `enter_held_messages` steering; adapter contract = SPEC-065.

## 7. Failure & degraded modes

- Engine down → typed degraded result spoken/typed ("voice unavailable"), gateway unaffected.
- Transport drop → client reconnect watcher (mirror `_reconnect.py`); no partial-turn commit.
- Pairing missing/expired → refuse with a spoken reason; never silent-allow (fail-closed).
- Federal tier → refuse-load (COMP-014).

---

*Implementation tasks with domain tags in `PLAN.md`.*
