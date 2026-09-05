"""Voice engine seam — cascade default; engines are config-selectable leaves.

Importing this package registers the built-in engines (piper, whisper, kokoro)
into the registry; config selects them by name and each validates its own config
sub-table. External packages add engines via the ``arcgateway.voice_engines``
entry-point group — no core change. PersonaPlex remains a deferred full-duplex leaf.
"""

# Import the leaf modules for their side effect: registering their factories.
from arcgateway.adapters.voice.engine import (
    kokoro as _kokoro,  # noqa: F401  registers "kokoro"
)
from arcgateway.adapters.voice.engine import stt as _stt  # noqa: F401  registers "whisper"
from arcgateway.adapters.voice.engine import tts as _tts  # noqa: F401  registers "piper"
from arcgateway.adapters.voice.engine.base import (
    FakeVoiceEngine,
    STTEngine,
    TTSEngine,
    VoiceEngine,
)
from arcgateway.adapters.voice.engine.registry import (
    build_stt,
    build_tts,
    registered_stt,
    registered_tts,
)

__all__ = [
    "FakeVoiceEngine",
    "STTEngine",
    "TTSEngine",
    "VoiceEngine",
    "build_stt",
    "build_tts",
    "registered_stt",
    "registered_tts",
]
