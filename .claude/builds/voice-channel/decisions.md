# Build Decisions: Voice Channel ("Hey Olivia")

**Feature:** voice-channel
**Date:** 2026-09-04
**Vision:** `.claude/brainstorms/2026-09-04-voice-channel.md`
**Memory:** `project_voice_channel_personaplex`
**Ranking:** every decision ordered by Simplicity → Modularity → Security → Scalability.
**Compliance:** regime `fedramp/nist` is declared, but this channel is **federal-forbidden**;
the binding controls are the universal Four Pillars (identity, sign, authorize, audit),
encrypt-at-rest, and PII redaction. Threat IDs from `CLAUDE.md` are cited per decision.

---

## The shape in one picture

> **Revised by /deepen (2026-09-04):** PersonaPlex cannot voice externally-authored
> text natively (it is an end-to-end speech-to-speech generator with no text input,
> single-session per GPU, ~160s context). Josh chose the **cascade**: STT → Arc agent
> → TTS, all behind the same `VoiceEngine` seam. PersonaPlex is demoted to an **optional,
> deferred full-duplex engine** behind that seam. Barge-in is delivered by the transport
> (WebRTC echo-cancel + Silero VAD), not by PersonaPlex — so nothing was lost.

```
[Thin audio client]                 [VoiceEngine = CASCADE on the DGX]
  mic + speaker                       STT (Whisper/Voxtral) ---transcript--->
  wake word (openWakeWord)                 ^                                    [gateway voice adapter] --dispatch--> [Arc agent]
  Silero VAD (endpoint)               TTS (Olivia-cloned)   <---final text----  AdapterSpec (SPEC-065)                tools/memory/
  Mac or the DGX itself                    |                                                                          policy/audit
        \____ WebRTC: Opus 20ms/VOIP, AEC + noise-supp, DataChannel for barge-in, mTLS ____/

  (PersonaPlex = optional full-duplex VoiceEngine impl behind the same seam — deferred)
```

Three roles, three seams. The TTS only voices; the Arc agent is the only brain.

---

## 1. Architecture

- **D-727 — First-party in-tree gateway adapter `arcgateway/adapters/voice/`** implementing
  `AdapterSpec` (SPEC-065), one agent per adapter, exactly like `telegram`/`slack`/`mattermost`.
  *Pillars:* Simplicity + Modularity — the gateway already owns audit, session identity,
  pairing, message splitting and media custody (`base.py` REQ-310); voice adds none of that.
  *Alt rejected:* a standalone service outside the gateway (re-implements the whole envelope,
  four more chances to forget a control).
- **D-728 — Three components behind seams:** (a) `AudioEndpoint` thin client (mic/speaker/wake),
  (b) `VoiceEngine` seam (PersonaPlex default, pluggable like the browser/arcllm seams),
  (c) the adapter bridging engine utterances to the agent. Deleting `adapters/voice/` + the
  client leaves every unrelated feature working; only voice goes away, as a typed absence.
  *Pillars:* Modularity (the seam is the product boundary).
- **D-729 — No relay.** Thin client ↔ DGX over Arc's own signed WebSocket; the **wake word runs
  on the client**, so audio only streams after wake. *Pillars:* Security — minimize exposure;
  an open mic never reaches the network (LLM07-adjacent, privacy-first).
- **D-730 — Content authority is the Arc agent; PersonaPlex never answers substantively**
  (thin-face). Every utterance runs through arcrun with full tools/memory/policy/audit.
  *Pillars:* Security — one brain, no shadow memory (ASI06 memory/context poisoning avoided).

## 2. Data Model

- **D-731 — No new durable content entity.** A voice turn is a normal session turn: a text
  Part in, a text Part out, landing in the existing session + audit stores. *Pillars:* Simplicity.
- **D-732 — Audio is never read into the envelope/session/prompt** (mirrors `PendingMedia`:
  the adapter handles bytes on the wire, they never become logged content). Transcripts persist
  **encrypted at rest** as part of the session; raw audio is **not** stored by default (opt-in
  debug flag only, encrypted, short TTL). *Threats:* LLM02/LLM07 (no sensitive audio in logs).
- **D-733 — `VoicePairing` record:** mic/endpoint identity → agent, tier, created-at, operator
  signature. Stored in the existing mechanical-approval grant store (SPEC-035), not a new table.
  *Pillars:* Simplicity + Security (IA).

## 3. API / Contracts

- **D-734 — Client↔DGX wire:** WebSocket carrying Opus audio frames + control messages
  (wake, end-of-utterance, barge-in), mTLS, with a signed pairing handshake carrying
  nonce+timestamp replay protection. *Threats:* ASI07 (inter-node comms), replay.
- **D-735 — Adapter↔agent is unchanged:** the existing gateway→agent dispatch of text Parts.
  No new agent-facing API. *Pillars:* Modularity — cross-layer calls use the public seam only.
- **D-736 — CLI surface mirrors Telegram:** `arc connect voice <agent>` (pair a mic),
  `arc voice start` (run the thin client). *Pillars:* Simplicity/consistency
  (see `connect-telegram`, `project_gateway_multibot_connect_telegram`).

## 4. Observability

- **D-737 — One OTEL span per voice turn** with latency marks: wake → ASR → dispatch →
  agent-run → TTS, plus `first_ack`, `first_token`, `done`. Metrics: wake events, false-wake
  rate, barge-ins, end-to-end latency. Reuses `core/telemetry.py`. *(stack-quality default.)*

## 5. Audit & Compliance

- **D-738 (auto-applied: fedramp/nist AU) — `arctrust.audit.emit` on every voice operation:**
  wake, utterance received, dispatch, response spoken, barge-in, pairing grant/refuse. Single
  emission point → `JsonlSink` + `SignedChainSink` + `UIBridgeSink`. Every event carries the
  `caller_did` and the mic/endpoint identity. *Threats:* ASI03 (identity), AU family.
- **D-739 — Federal-forbidden by construction:** the adapter **refuses to load on the federal
  tier** via a config gate, not a code branch. *Threats:* declared scope constraint; desk mic
  is out of threat model for SCIF.

## 6. Security

- **D-740 — Tier gate:** loads on personal + enterprise only (see D-739).
- **D-741 — Pairing trust model:** personal = one signed mic pairing at setup (operator key);
  enterprise = **TOFU** — first use raises a mechanical-approval pending row, `arc approve`
  signs it, thereafter the mic is trusted. Reuses SPEC-035. *Threats:* IA, ASI03.
- **D-742 — Trusted mic is still untrusted *content*.** The spoken utterance is user-adjacent
  text, never control-plane (LLM01 prompt injection). Consequential/irreversible actions still
  pass `PolicyPipeline` and a human-approval gate; the confirmation-first spoken contract (D-748)
  reinforces this. *Threats:* LLM01, LLM05, LLM06/ASI02, ASI09, lethal trifecta.
- **D-743 — Transport & keys:** mTLS on the client↔DGX WS (ASI07); operator/agent keys
  non-exportable (capability handle, never raw material); replay protection on the handshake.
- **D-744 — Encrypt transcripts (and any opt-in audio) at rest.** *(Data-protection mandate.)*
- **D-745 — PersonaPlex is a signed, provenance-checked model artifact** verified at load,
  even though MIT-licensed. *Threats:* LLM03 / ASI04 supply chain.

## 7. Integration

- **D-746 — PersonaPlex behind a `VoiceEngine` Protocol**, default impl `PersonaPlexEngine`;
  the optional NVIDIA dependency is imported **inside the plugin boundary only** and never
  leaks into contract types. Engine down → typed degraded result (spoken/typed "voice
  unavailable"), not an import error. Circuit breaker + timeout on the engine. *Pillars:*
  Modularity + Scalability (fail gracefully).
- **D-747 — Barge-in reuses existing steering** (`enter_held_messages` drains at the turn
  boundary, `project_steering_unified_one_rule`). Talking over Olivia = steering an in-flight
  turn. No new steering path invented. *Pillars:* Simplicity.

## 8. Performance

- **D-748 — Fast audible ack decoupled from the turn.** The adapter emits a spoken "on it"
  the instant an utterance is dispatched, via PersonaPlex, independent of the ~97s agent turn;
  the real answer follows. Silence during a slow turn reads as broken. *Pillars:* the "never
  silent" principle.
- **D-749 — Wake word: openWakeWord (MIT), local, lightweight.** Chosen over Picovoice
  Porcupine to avoid a proprietary license/key server — friendlier to CMMC supply-chain and to
  offline/local-first. Only streams post-wake. *Pillars:* Security + Simplicity.
- **D-750 — One PersonaPlex instance per DGX**, shared across that box's voice sessions;
  target first-ack < 1s (PersonaPlex ~170ms leaves headroom). *Pillars:* Scalability.

## 9. Extensibility

- **D-751 — Config `[adapters.voice]` per agent**; the `VoiceEngine` and `AudioEndpoint` seams
  let PersonaPlex→Riva and the wake engine be swapped without touching callers. Toggle by tier
  + config. *Pillars:* Modularity.
- **D-752 — Short-form spoken output contract as a signed arcprompt overlay** scoped to the
  voice channel (`project_arcprompt_shipped`): succinct answers, confirmation-first, "here's
  what I did" summaries — built for the ear, not the eye. It is a **protected artifact** the
  model cannot edit (LLM07). *Origin:* Josh's v1-win note; a text-vs-voice output contract
  (cf. `feedback_prompt_reply_is_the_artifact`).

## 10. Testing

- **D-753 — Contract + architecture tests:** `VoiceEngine` contract run against
  `PersonaPlexEngine` and a `FakeVoiceEngine`; an architecture test proves the gateway starts
  with `adapters/voice/` **physically absent**. *Pillars:* the seam must be deletable.
- **D-754 — One journey test** from a real granted pairing → spoken utterance → agent run →
  spoken reply, faking only the **audio wire and the LLM wire** (`feedback_test_what_users_do`).
- **D-755 — Abuse battery** into `scripts/run_adversarial_tests.py`: forged/replayed pairing,
  replayed audio frame, wake-word spoof from a recording, federal-tier load attempt, injection
  via spoken content, stolen WS handle, barge-in flooding, TOFU race. Gates: 80/75/90.

## 11. Deployment

- **D-756 — PersonaPlex weights installed on the DGX as a signed bundle;** thin client ships in
  `arccli`. Deploy from `main` (`project_deploy_always_from_main`); personal-tier deploy
  rebuilds bundles from source (`project_deploy_stale_module_bundles`). Rollback = disable
  `[adapters.voice]` + stop the client.
- **D-757 — `arc voice start` runs as a systemd service on the desk box;** headless DGX sets
  `DBUS_SESSION_BUS_ADDRESS=/dev/null` so the keyring never hangs the client
  (`project_headless_node_keyring_hangs_clis`).

## 12. UI/UX

- **D-758 — Audible-only interaction:** chime on wake, spoken "on it" ack, short spoken answer,
  spoken confirmation before consequential actions, barge-in to interrupt.
- **D-759 — Reply stays with the voice origin** (`feedback_reply_stays_with_origin_channel`):
  a voice turn is answered by voice, never leaked to Telegram.
- **D-760 — arcui surfaces the voice channel + pairing status on the connection card**, parity
  with connector cards (the four-pillars "a connection is not complete" principle). Optional,
  not required for v1 talk-and-answer.

---

## The genuine tradeoff Josh resolved

**Mac + GPU:** PersonaPlex needs NVIDIA; the Mac has none. Chosen: **Mac = thin client, DGX
runs PersonaPlex** (audio streams to the DGX over the LAN, mTLS). Keeps "start it anywhere,"
keeps audio + model on Josh's own hardware, one PersonaPlex to run. Rejected: DGX-only v1
(Mac can't wake Olivia); a second CPU voice path on Mac (worse voice, two code paths, fights
thin-face).

---

## Research Insights & Resolutions (/deepen — 2026-09-04)

Three parallel Explore researchers. Findings filtered by Simplicity → Modularity →
Security → Scalability. Each carries its scalability ceiling, security posture, and
module-boundary note. Full source lists live in the research briefs; key URLs inline.

### R-1 — PersonaPlex is not a thin-face TTS → cascade (supersedes D-730's engine, reshapes D-746)

- **Finding:** `nvidia/personaplex-7b-v1` is Moshi-based, full-duplex speech-to-speech,
  **no text-input** — its `text_prompt` sets persona only, content is ignored. Making it
  speak Arc's words needs an undocumented inner-monologue drip-feed that repetition-collapses
  and garbles. Reference server is **single-session per GPU** (`asyncio.Lock`, ~14–19 GB VRAM),
  context ~163 s, and "interrupt" is learned behavior with no cancel API (you mute frames).
  ([HF discussion #2](https://huggingface.co/nvidia/personaplex-7b-v1/discussions/2),
  [VAOS bridge gist](https://gist.github.com/jmanhype/5aefd67d9e67b37a8b408abdab39b6d3),
  [NVIDIA ADLR](https://research.nvidia.com/labs/adlr/personaplex/))
- **D-761 (decision, Josh):** the default `VoiceEngine` is a **cascade** — `STTEngine`
  (Whisper/Voxtral) → Arc agent → `TTSEngine` (voice cloned to sound like Olivia).
  PersonaPlex becomes an **optional `FullDuplexEngine`** behind the same `VoiceEngine` seam,
  deferred. *Simplicity+Modularity+Security+Scalability all favor cascade:* every stage is a
  debuggable, swappable box, fully self-hosted, and STT/TTS scale statelessly with **no
  1-GPU-per-call ceiling**. Thin-face is now true **by construction** — Arc authors the text,
  the TTS only voices it.
- **Module boundary:** `VoiceEngine` = one Protocol, two default sub-seams (`STTEngine`,
  `TTSEngine`); PersonaPlex, if ever enabled, implements `VoiceEngine` wholesale. Optional
  model deps (Whisper/PersonaPlex) stay inside the engine boundary, never in contract types.
- **Scalability ceiling:** cascade = the DGX's parallel STT/TTS throughput (horizontal).
  The PersonaPlex option, if enabled later, reintroduces the single-session-per-GPU cap —
  documented as its known ceiling.

### R-2 — Transport is WebRTC, not raw WebSocket (supersedes D-734)

- **Finding:** full-duplex barge-in over **open speakers** needs acoustic echo cancellation,
  or Olivia's own TTS leaks into the mic and false-triggers the VAD. **WebRTC** ships AEC +
  noise-suppression + jitter buffer + VAD and targets the sub-500 ms budget; a bare
  WebSocket gives none of that and TCP head-of-line blocking adds jitter.
  ([LiveKit](https://livekit.com/blog/why-webrtc-beats-websockets-for-voice-ai-agents))
- **D-762 (supersedes D-734):** client↔DGX media over **WebRTC** (`aiortc`, or LiveKit if we
  later want managed signaling), **Opus 20 ms / `OPUS_APPLICATION_VOIP` / ~24 kbps VBR**,
  24 kHz; an **RTCDataChannel** carries control (wake, end-of-utterance, barge-in). *Security:*
  media is **DTLS-SRTP encrypted**; pin DTLS fingerprints, run signaling + DataChannel over
  **mTLS**, **host candidates only — no external STUN/TURN** (keeps the CMMC boundary tight,
  ASI07). *Scalability ceiling:* aiortc handles a handful of peers; many desks → a LiveKit
  SFU, a server-side change, not a client rewrite.

### R-3 — Endpointing + wake word (refines D-749, adds D-763)

- **Finding:** **openWakeWord** supports a self-trained "hey Olivia" (Colab, synthetic data,
  ONNX out); < 5 % false-reject, < 0.5 false-accepts/hr with tuning; negligible CPU; runs on
  macOS via ONNX Runtime. **License footnote:** prebuilt models are CC BY-NC-SA — **train our
  own** so the NC clause isn't inherited; pin ONNX Runtime + vendor the model file.
  ([openWakeWord](https://github.com/dscripka/openWakeWord)) For "you're done talking,"
  **Silero VAD** on echo-cancelled audio (misses ~12 % of speech frames vs WebRTC-VAD's ~50 %
  at 5 % FP) with a tuned ~500–800 ms trailing-silence timeout; WebRTC's built-in VAD as the
  cheap always-on gate.
  ([Picovoice VAD](https://picovoice.ai/blog/best-voice-activity-detection-vad/))
- **D-763 (adds to D-749):** wake = openWakeWord (self-trained, ONNX, on the client);
  endpoint = **Silero VAD** on the server over AEC'd audio + tuned hangover; WebRTC-VAD as the
  cheap gate. *Ops:* grant macOS **mic (TCC) permission** to the client binary (a headless
  launchd process fails silently otherwise); on Linux install system `libportaudio2` and pin
  an explicit ALSA device. *Security:* dropping Picovoice removes a third-party key-activation
  dependency (supply-chain win); vet/pin the model + runtime so it isn't undercut by an
  unpinned pip.

### R-4 — Four thin UX modules (refines D-748, D-752, D-742, D-758)

Voice-UX research says: do **not** scatter this in prompts or per-skill code — four reusable,
event/state-driven modules. ([NN/g audio signifiers](https://www.nngroup.com/articles/audio-signifiers-voice-interaction/),
[Ultravox latency](https://www.ultravox.ai/voice-ai/understanding-latency-in-voice-ai-systems),
[Hamming barge-in runbook](https://hamming.ai/resources/voice-agent-interruption-handling-runbook))

- **D-764 — Voice Output Contract (supersedes D-752):** a signed arcprompt overlay sets the
  *style*, **and a code post-processor enforces it** — hard word cap, strip markdown/links,
  collapse lists to "and a few more — want the rest?", split spoken-vs-detail. A prompt alone
  is unenforceable and injection-bypassable; the **code filter is also the security boundary**
  on what untrusted text gets read aloud (LLM05/LLM07).
- **D-765 — Latency/Progress Manager (supersedes D-748):** audible silence must never exceed
  ~1 s or it reads as broken. Instant ack ("on it"), then **periodic earcon/heartbeat cues**
  during the ~97 s turn, then a **hard-timeout failure message** — never infinite "thinking."
  Wraps every long-running dispatch; fillers stay generic and **never narrate unverified
  backend content mid-flight**.
- **D-766 — Confirmation Gate (supersedes/hardens D-742):** risk-tiered, between intent and
  tool execution. Reversible actions → implicit confirm ("Playing…"); **irreversible/
  consequential → explicit read-back of the *actual* parameters + bounded yes/no; ambiguity or
  silence → abort.** This is the **load-bearing security guard** — the human is the commit
  authority against both misheard *and* injected/agent-initiated actions (LLM01/ASI02/ASI09,
  lethal trifecta). Risk metadata rides on each tool so the gate covers new tools automatically.
- **D-767 — Audio-Feedback + Interruption (supersedes D-758's cues, refines D-747):** a fixed
  earcon set — wake (one-shot), listening (soft loop), working, done, error — driven by dialogue
  state events. Barge-in: **stop on confirmed speech**, but **distinguish backchannels
  ("mm-hmm") from real interrupts** (a false cut-off erodes trust faster than a missed one), and
  **raise the barge-in confirmation bar during a confirmation read-back** so noise cannot commit
  or cancel a consequential action. Barge-in still redirects in-flight content via the existing
  `enter_held_messages` steering (D-747).

### Push-to-talk (resolves the last open item)

- **D-768:** Mac gets **push-to-talk as a wake fallback** (it has a keyboard); the DGX is
  wake-word only. Both funnel to the same "utterance start" event, so the engine seam is
  unchanged.

---

## The genuine tradeoffs, resolved

1. **Mac + GPU (Josh):** Mac = thin client, DGX runs the engine; audio streams over the LAN
   (now WebRTC/mTLS). Rejected DGX-only-v1 and a second CPU voice path.
2. **Voice engine (Josh, post-research):** **cascade now, PersonaPlex optional later.**
   Rejected gated-duplex-PersonaPlex (1 GPU/convo, fragile) and the drip-feed spike (brittle).

*Next: `/specify voice-channel` — the decisions and research above seed the PRD/SDD/PLAN.*
