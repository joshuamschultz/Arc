"""Decorator-form voice module — SPEC-021 task 3.4.

Two ``@tool`` functions and two ``@hook`` subscribers that mirror the
legacy :class:`VoiceModule` surface:

  * ``@tool transcribe(audio_path, language=None)`` — STT call,
    PII redaction at federal/enterprise tiers, audit on completion.
  * ``@tool synthesize(text, voice_id=None)`` — TTS call into a
    unique tempfile path, redaction on input, audit on completion.
  * ``@hook voice.transcribe.request`` — bus-driven entrypoint that
    invokes the transcribe pipeline.
  * ``@hook voice.synthesize.request`` — bus-driven entrypoint that
    invokes the synthesize pipeline.

State is shared via :mod:`arcagent.modules.voice._runtime`. The agent
configures it once at startup; the tools and hooks read state lazily.

The legacy :class:`VoiceModule` class still exists alongside this
module to keep its existing test surface working; both forms route
to the same provider plugins and audit semantics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from arcagent.modules.voice import _runtime
from arcagent.modules.voice.errors import STTFailed, TTSFailed
from arcagent.modules.voice.protocols import TranscriptionResult
from arcagent.modules.voice.redaction import redact_transcript
from arcagent.tools._decorator import hook, tool
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.voice.capabilities")

# Bound exception detail length to keep structured logs from ballooning
# on large exception messages (LLM02).
_MAX_ERROR_MSG_LEN: int = 200


@tool(
    name="transcribe",
    description=(
        "Transcribe an audio file to text using the configured STT provider. "
        "Returns the transcript text, detected language, and audio duration. "
        "PII is redacted automatically at federal/enterprise tiers."
    ),
    classification="state_modifying",
    capability_tags=["audio", "stt", "transcription"],
    when_to_use="When you need to convert an audio file into text.",
    version="1.0.0",
)
async def transcribe(audio_path: str, language: str | None = None) -> str:
    """Transcribe an audio file. Returns a JSON string for tool transport."""
    result = await _transcribe(Path(audio_path), language=language)
    return json.dumps(result)


@tool(
    name="synthesize",
    description=(
        "Synthesize text to an audio file using the configured TTS provider. "
        "Returns the path to the generated audio file. "
        "PII is redacted from the input text at federal/enterprise tiers."
    ),
    classification="state_modifying",
    capability_tags=["audio", "tts", "synthesis"],
    when_to_use="When you need to render text as spoken audio.",
    version="1.0.0",
)
async def synthesize(text: str, voice_id: str | None = None) -> str:
    """Synthesize text to a unique tempfile. Returns a JSON string."""
    out_name = f"arc_tts_{uuid.uuid4().hex}.mp3"
    output_path = Path(tempfile.gettempdir()) / out_name
    result = await _synthesize(text, voice_id=voice_id, output_path=output_path)
    return json.dumps(result)


@hook(event="voice.transcribe.request")
async def on_transcribe_request(ctx: Any) -> None:
    """Handle ``voice.transcribe.request`` bus events.

    Reads ``audio_path`` (and optional ``language``) from ``ctx.data``
    and invokes the transcription pipeline. The transcript hash is
    placed back on ``ctx.data['result']`` so callers can correlate
    without exposing raw text on the bus.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    audio_path = data.get("audio_path")
    if not audio_path:
        return
    language = data.get("language")
    result = await _transcribe(Path(audio_path), language=language)
    data["result"] = result


@hook(event="voice.synthesize.request")
async def on_synthesize_request(ctx: Any) -> None:
    """Handle ``voice.synthesize.request`` bus events.

    Reads ``text`` (and optional ``voice_id`` / ``output_path``) from
    ``ctx.data`` and invokes the synthesis pipeline. When no
    ``output_path`` is supplied, a unique tempfile is generated.
    """
    data = ctx.data if hasattr(ctx, "data") else {}
    text = data.get("text")
    if not text:
        return
    voice_id = data.get("voice_id")
    output_path_raw = data.get("output_path")
    if output_path_raw:
        output_path = Path(output_path_raw)
    else:
        out_name = f"arc_tts_{uuid.uuid4().hex}.mp3"
        output_path = Path(tempfile.gettempdir()) / out_name
    result = await _synthesize(text, voice_id=voice_id, output_path=output_path)
    data["result"] = result


# --- Internal pipelines (shared by tools and hooks) ----------------------


async def _transcribe(
    audio_path: Path,
    *,
    language: str | None = None,
) -> dict[str, Any]:
    """Run the full transcription pipeline with redaction and audit."""
    st = _runtime.state()
    stt = _runtime.get_stt_provider()

    try:
        result: TranscriptionResult = await stt.transcribe(audio_path, language=language)
    except STTFailed:
        raise
    except Exception as exc:
        raise STTFailed(
            f"Unexpected error during transcription: {type(exc).__name__}",
            details={"error": str(exc)[:_MAX_ERROR_MSG_LEN]},
        ) from exc

    text = result.text
    redaction_applied = False
    if st.config.effective_redact_pii:
        text, redaction_applied = redact_transcript(text)

    transcript_hash = hashlib.sha256(text.encode()).hexdigest()
    await safe_audit(
        st.telemetry,
        "voice.transcribed",
        {
            "provider": st.config.stt_provider,
            "duration_s": result.duration_s,
            "language": result.language,
            "redaction_applied": redaction_applied,
            "transcript_hash": transcript_hash,
        },
        logger=_logger,
    )

    return {
        "text": text,
        "language": result.language,
        "duration_s": result.duration_s,
    }


async def _synthesize(
    text: str,
    *,
    voice_id: str | None = None,
    output_path: Path,
) -> dict[str, Any]:
    """Run the full synthesis pipeline with redaction and audit."""
    st = _runtime.state()
    tts = _runtime.get_tts_provider()

    synthesis_text = text
    if st.config.effective_redact_pii:
        synthesis_text, _ = redact_transcript(text)

    text_hash = hashlib.sha256(synthesis_text.encode()).hexdigest()

    try:
        out_path = await tts.synthesize(synthesis_text, voice_id=voice_id, output_path=output_path)
    except TTSFailed:
        raise
    except Exception as exc:
        raise TTSFailed(
            f"Unexpected error during synthesis: {type(exc).__name__}",
            details={"error": str(exc)[:_MAX_ERROR_MSG_LEN]},
        ) from exc

    output_size_bytes = out_path.stat().st_size if out_path.exists() else 0

    await safe_audit(
        st.telemetry,
        "voice.synthesized",
        {
            "provider": st.config.tts_provider,
            "voice_id": voice_id or "default",
            "text_hash": text_hash,
            "output_size_bytes": output_size_bytes,
        },
        logger=_logger,
    )

    return {"audio_path": str(out_path)}
