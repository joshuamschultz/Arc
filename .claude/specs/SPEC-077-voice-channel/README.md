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
| PLAN | IN PROGRESS — Phase 1 COMPLETE (T-001..006, verified); Phases 2–4 pending |
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

### Phases 2–4 — not started

Need real WebRTC (aiortc), audio hardware, and GPU models (Whisper/TTS); they can't
be built-and-verified in a non-hardware session and were not attempted.
