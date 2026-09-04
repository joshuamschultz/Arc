# Brainstorm: Desk Voice Channel ("Hey Olivia")

**Date:** 2026-09-04
**Topic:** voice-channel
**Status:** vision captured → ready for `/build`

---

## Inspiration

Opening Telegram or the web dashboard to reach an agent is too slow when you're
already working at the desk. The friction of switching apps and typing kills the
impulse to just ask. Josh wants to turn to the machine and talk: say "hey Olivia,"
she wakes, he speaks a request, she runs it and talks back. Same thing Telegram
already does, but spoken instead of typed, so a quick request never means stopping
what you're doing.

## Audience

Both tiers, designed together from day one:

- **Personal (Josh first).** His desk. A DGX (no keyboard, wake-word only) and/or
  his Mac. The primary user and the reason to build.
- **Enterprise operators.** Same channel, TOFU pairing and approval, so it ships
  as a real product feature, not a personal hack.
- **Explicitly NOT federal.** A desk microphone is too high-risk for SCIF/lab use.
  The channel is forbidden on the federal tier by config, and refuses to load there.

## Use Cases

1. **Quick request mid-work.** "Hey Olivia, what did the DGX cost run at today?"
   → spoken answer, no app switch, no typing.
2. **Kick off an agentic run by voice.** Same as sending a Telegram message: the
   request routes to the real Arc agent, which runs its loop with full tools/memory.
3. **Hands-free while doing something else** at the desk — reading, on a call, working
   on another screen.
4. **Confirm and steer.** Because voice can't show a diff, the agent asks for spoken
   confirmation before consequential actions, and you can interrupt (barge-in) to
   redirect it mid-run.

## Desired Outcomes

- The fastest possible path from "I have a request" to "the agent is working on it."
- Voice is just another gateway channel: bound to one agent, reply stays with the
  voice origin, same agent brain/memory/audit as Telegram.
- Responses are built for the ear, not the eye:
  - **Succinct by default** — no reading a wall of text aloud.
  - **More confirmations** — check before doing, since there's no screen to review.
  - **Quick "here's what I did" / short answers** rather than full transcripts.
- Feels human: low latency, and you can talk over Olivia to interrupt.

## Guiding Principles

1. **Thin face, real brain.** PersonaPlex only talks and handles barge-in. Every
   request routes to the real Arc agent over arcrun. No second brain, no shadow memory.
2. **Local first.** Wake word, mic, speaker, and the PersonaPlex model all run on the
   operator's own hardware. Audio never leaves the box until (and only if) the agent
   itself reaches out. This is the privacy story that makes personal + enterprise safe.
3. **Trust the mic, but prove it once.** Personal = one signed pairing at setup.
   Enterprise = TOFU, approve the mic once. After that the mic is a trusted source.
4. **Never silent.** A chime on wake, a fast spoken "on it" the instant a request
   lands, then the real answer. Silence during a slow (~97s) turn reads as broken.
5. **A voice-output contract.** The agent's spoken replies obey a short-form,
   confirmation-first contract distinct from its text replies. (Same idea as the
   text output contract in `feedback_prompt_reply_is_the_artifact`.)
6. **Federal-forbidden by construction**, not by policy note. Tier-gated so it can't
   load where it isn't allowed.
7. **Audit everything, encrypt at rest.** Every voice turn is an event; transcripts
   and audio are sensitive data.

## Constraints & Scope

**In scope (v1):**
- New gateway adapter, one agent per adapter (Telegram multibot pattern).
- Local client: wake word + mic capture + speaker playback, cross-platform
  (PortAudio / `sounddevice`). Attaches to the remote agent — no relay.
- PersonaPlex (`nvidia/personaplex-7b-v1`, MIT, local, full-duplex, ~170ms) as the
  voice layer; barge-in mapped onto existing steering (`enter_held_messages`).
- Ask → route to Arc agent → run → speak a short answer. Parity with a Telegram
  message, spoken.
- Personal signed pairing + enterprise TOFU; tier gate blocking federal.
- Per-turn audit, encryption at rest, short-form spoken output contract.

**Out of scope (later):**
- Dedicated hardware puck.
- PersonaPlex answering anything on its own (thin-face only).
- Any federal-tier voice.

**Open design questions (for `/build`):**
- The exact PersonaPlex ↔ Arc-agent handoff seam (the core work).
- DGX audio hardware: USB mic/speaker + ALSA; `DBUS_SESSION_BUS_ADDRESS=/dev/null`.
- Push-to-talk on Mac as a wake-word fallback; wake-word engine choice.
- How the short-form spoken output contract is expressed and enforced.
- Endpointing (knowing you're done talking) and confirmation UX by ear.

---

*Related memory: `project_voice_channel_personaplex`. Next: `/build voice-channel`.*
