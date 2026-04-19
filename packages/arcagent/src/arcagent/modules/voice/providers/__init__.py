"""Voice provider implementations.

Each submodule implements either STTProvider or TTSProvider protocol.

Air-gap (no network):
    whisper_cpp  — STT via Whisper.cpp subprocess
    piper        — TTS via Piper subprocess

Cloud (requires credentials):
    whisper_api  — STT via OpenAI Whisper API (via arcllm or httpx)
    elevenlabs   — TTS via ElevenLabs REST API (via httpx)

All providers are STUB implementations for providers whose binary/SDK
is not guaranteed to be installed. Each stub raises a clear error with
instructions rather than failing opaquely.
"""
