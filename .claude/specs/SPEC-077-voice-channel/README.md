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
| PLAN | PENDING — 36 tasks (T-001..036), 4 phases, domain-tagged, TDD |
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

_None yet._
