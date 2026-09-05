# Voice Channel — DGX / GPU Deploy Guide (SPEC-077)

How to stand up the "hey Olivia" voice channel on a GPU box (DGX Spark / any
NVIDIA host) and its thin client on a Mac. Verified on `spark-0290` (NVIDIA GB10,
aarch64) on 2026-09-04.

> **Status:** the cascade **engine** (STT + TTS) is built and verified against real
> models. The **transport** (WebRTC), **wake word** (openWakeWord), and the
> **pairing/audit wiring** are not wired yet — see *Not yet wired* at the bottom.
> This guide covers what works today plus the durable setup steps.

---

## 1. Architecture recap

```
[thin client: Mac or desk box]        [engine host: the GPU box]
  mic + speaker + wake word    --->    faster-whisper (STT)  --->  Arc agent
  (openWakeWord, PortAudio)    <---    Piper (TTS, Olivia voice) <--  reply text
        WebRTC (Opus, AEC, mTLS) over the LAN
```

- The **engine host** needs the GPU/CPU and the models. It runs the gateway with
  the voice adapter.
- The **thin client** needs a mic and speaker (a headless server has neither), so
  it runs on the Mac / desk box. The DGX is engine-side only.
- **Federal tier is forbidden** — the adapter refuses to load there.

## 2. Prerequisites

- Python 3.12 (3.11+ works).
- On the engine host: an NVIDIA GPU (CPU int8 also works for whisper-tiny/small).
- Outbound network for the one-time model pulls (HuggingFace + Piper voices).
- Headless boxes: export `DBUS_SESSION_BUS_ADDRESS=/dev/null` so keyring calls
  don't hang CLIs.

## 3. Install the voice stack

The whole stack is declared as an optional extra. **Models are never vendored** —
only the code and the dependency list ship; models are pulled per box.

**Recommended: an isolated venv** (keeps the live fleet's environment untouched):

```bash
python3 -m venv ~/voicedev/.venv
. ~/voicedev/.venv/bin/activate
pip install --upgrade pip
pip install 'arcgateway[voice]'      # faster-whisper, piper-tts, aiortc, openwakeword, ...
```

Or install into the fleet env if you intend the running gateway to serve voice:
`pip install 'arcgateway[voice]'` in that env.

**ARM / GB10 note:** all wheels resolved on aarch64 with no build step
(faster-whisper uses CTranslate2, not torch). No CUDA build was required for the
CPU int8 path.

## 4. Pull the models (per box, one time)

```bash
# TTS — the Olivia voice (swap the voice name to change how she sounds)
mkdir -p ~/voicedev/voices
python -m piper.download_voices en_US-lessac-medium --download-dir ~/voicedev/voices
#   -> en_US-lessac-medium.onnx  (+ .onnx.json)

# STT — faster-whisper pulls the model on first use; pre-pull by loading it once:
python - <<'PY'
from faster_whisper import WhisperModel
WhisperModel("tiny", device="cpu", compute_type="int8")   # or "small" for accuracy
PY
```

Model size vs. accuracy: `tiny` is fast and was accurate enough in testing;
`small`/`medium` improve accuracy at higher latency/VRAM.

## 5. Configure the channel

Add a `[platforms.voice]` block to the gateway's `gateway.toml`, one per agent
(same multi-bot pattern as Telegram):

```toml
[platforms.voice]
enabled   = true
agent_did = "did:arc:olivia"          # the agent this mic talks to
chat_id   = "olivia"                   # MUST NOT contain ':' (collides with the
                                       # platform:chat_id:thread reply address)

# engine (cascade) settings — consumed by the adapter in later phases
[platforms.voice.engine]
stt_model    = "tiny"                  # or a local model path
stt_device   = "cpu"                   # "cuda" once the CTranslate2 CUDA build is in
stt_compute  = "int8"
tts_voice    = "/home/<user>/voicedev/voices/en_US-lessac-medium.onnx"
```

> Personal/enterprise accept a **self-signed local model** (audit warns). To pin a
> model, add its `sha256`; the artifact verifier then refuses anything that does
> not match (fail-closed). Cryptographic (arctrust/Sigstore) signing of model
> artifacts is the deferred integration.

## 6. Verify (round-trip smoke)

Proves TTS + STT work end-to-end through the real classes:

```bash
. ~/voicedev/.venv/bin/activate
export DBUS_SESSION_BUS_ADDRESS=/dev/null
python - <<'PY'
import io, wave, glob, asyncio, numpy as np
from arcgateway.adapters.voice.engine.tts import PiperTTS
from arcgateway.adapters.voice.engine.stt import WhisperSTT

onnx = glob.glob("<HOME>/voicedev/voices/*lessac-medium*.onnx")[0]
def to16k(wav):
    with wave.open(io.BytesIO(wav)) as w:
        sr, pcm = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    if sr != 16000:
        pcm = np.interp(np.linspace(0, len(pcm)-1, int(len(pcm)*16000/sr)), np.arange(len(pcm)), pcm)
    return pcm.astype(np.int16).tobytes()

async def main():
    wav = await PiperTTS(voice_path=onnx).speak("hey olivia what is on my calendar today")
    heard = await WhisperSTT(model="tiny", device="cpu", compute_type="int8").listen(to16k(wav))
    print("HEARD:", heard)
asyncio.run(main())
PY
```

Expected: the phrase comes back (e.g. `hey olivia, what is on my calendar today?`).

## 7. The Mac thin client

Runs the mic/speaker/wake word and streams to the engine host.

- Install the extra on the Mac too (libs only, no models needed for the client):
  `pip install 'arcgateway[voice]'`.
- **macOS microphone permission (TCC):** grant mic access to the process that runs
  the client (Terminal / the app bundle / the Python binary) in
  System Settings → Privacy & Security → Microphone, or capture returns silence
  with no prompt (a headless launchd process fails silently).
- **Linux desk box:** install system PortAudio (`libportaudio2`) and pin an explicit
  ALSA device rather than the default.

## 8. Security & operations

- Federal tier: the adapter refuses to load (desk mic is out of the SCIF threat
  model). Personal = one signed pairing; enterprise = TOFU approve-once.
- Transcripts are encrypted at rest; raw audio is not persisted by default; audio
  never enters the session log or the prompt.
- GPU contention: on a shared box, size the whisper model so inference does not
  starve the live fleet.

## 9. Run it today

The engine (STT + TTS), the WebSocket transport, pairing and per-turn audit are
wired. To talk:

1. **Set the pairing token** on the engine host (a credential — env, `0600`, never
   config): `export ARC_VOICE_TOKEN=$(openssl rand -hex 32)`.
2. **Enable `[platforms.voice]`** in the running gateway's `gateway.toml` (§5) and
   restart the gateway. It now serves the voice channel for that agent.
3. **On the Mac (client):** `pip install 'arcgateway[voice]'`, then:
   ```bash
   export ARC_VOICE_URI="ws://<engine-host>:8790"
   export ARC_VOICE_TOKEN="<the same token>"
   arc-voice          # push-to-talk: Enter, speak, Enter — hear the reply
   ```

Verify the whole loop first with the §6 round-trip smoke; then `arc-voice` for the
live push-to-talk experience.

## 10. Run the client under launchd / systemd

The client is push-to-talk (interactive), so it is usually run in a terminal. To
keep it resident, wrap `arc-voice` in a user service. Linux desk box:

```ini
# ~/.config/systemd/user/arc-voice.service
[Unit]
Description=Arc desk voice client
[Service]
Environment=ARC_VOICE_URI=ws://<engine-host>:8790
Environment=ARC_VOICE_TOKEN=<token>
Environment=DBUS_SESSION_BUS_ADDRESS=/dev/null
ExecStart=%h/.local/bin/arc-voice
Restart=on-failure
[Install]
WantedBy=default.target
```

`systemctl --user enable --now arc-voice`. On macOS use a launchd agent, and grant
the launching binary microphone access (TCC) — a background agent gets no prompt.

## 11. Not yet wired (roadmap)

- **Wake word model:** the `WakeGate` logic and push-to-talk are wired; still to do
  is training a "hey Olivia" openWakeWord ONNX model (do not ship the CC-BY-NC
  prebuilt voices) and feeding its frames to the gate on the client.
- **WebRTC upgrade (D-762):** replace the v1 WebSocket with `aiortc` — Opus/AEC,
  DataChannel barge-in, DTLS-SRTP + **mTLS**, host-only candidates — for
  talk-over-the-assistant barge-in on open speakers.
- **Enterprise TOFU pairing:** approve-once via the SPEC-035 grant store (v1 uses a
  single operator token).
- **arctrust model signing:** cryptographic Sigstore/arctrust signatures on model
  artifacts (v1 verifies content digest + accepts self-signed local models).
- **arcui connection card:** voice pairing status per agent (cosmetic; REQ-023 Could).
