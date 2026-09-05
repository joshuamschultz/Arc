"""Voice engine registry — swap engines by config, not by a core branch.

The seam is the product boundary (build-principles §4). A TTS or STT engine is a
leaf that *registers* itself under a name; config selects it by that name and
passes it an engine-specific config sub-table which the leaf validates itself
(exactly how a gateway AdapterSpec validates its own ``raw_config``). Adding
ElevenLabs, XTTS, Riva, PersonaPlex, … means dropping in a leaf that registers —
never editing an ``if engine == ...`` branch here.

Built-in engines register on import (see ``engine/__init__``). External packages
register via the ``arcgateway.voice_engines`` entry-point group, so a pip-installed
extension adds a voice engine with zero core changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcgateway.adapters.voice.engine.base import STTEngine, TTSEngine

TTSFactory = Callable[[Mapping[str, Any]], "TTSEngine"]
STTFactory = Callable[[Mapping[str, Any]], "STTEngine"]

_TTS: dict[str, TTSFactory] = {}
_STT: dict[str, STTFactory] = {}
_ENTRY_POINTS_LOADED = False


class UnknownVoiceEngineError(RuntimeError):
    """Config named a TTS/STT engine that no leaf has registered."""


def register_tts(name: str) -> Callable[[TTSFactory], TTSFactory]:
    def _register(factory: TTSFactory) -> TTSFactory:
        _TTS[name] = factory
        return factory

    return _register


def register_stt(name: str) -> Callable[[STTFactory], STTFactory]:
    def _register(factory: STTFactory) -> STTFactory:
        _STT[name] = factory
        return factory

    return _register


def _load_entry_points() -> None:
    """Let pip-installed extensions register engines (group arcgateway.voice_engines)."""
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="arcgateway.voice_engines"):
            register = ep.load()  # the entry point calls register_tts/register_stt
            if callable(register):
                register()
    except Exception:  # reason: a broken third-party engine must not kill startup
        return


def build_tts(name: str, config: Mapping[str, Any]) -> TTSEngine:
    _load_entry_points()
    factory = _TTS.get(name)
    if factory is None:
        raise UnknownVoiceEngineError(f"unknown TTS engine {name!r}; registered: {sorted(_TTS)}")
    return factory(config)


def build_stt(name: str, config: Mapping[str, Any]) -> STTEngine:
    _load_entry_points()
    factory = _STT.get(name)
    if factory is None:
        raise UnknownVoiceEngineError(f"unknown STT engine {name!r}; registered: {sorted(_STT)}")
    return factory(config)


def registered_tts() -> list[str]:
    _load_entry_points()
    return sorted(_TTS)


def registered_stt() -> list[str]:
    _load_entry_points()
    return sorted(_STT)


__all__ = [
    "STTFactory",
    "TTSFactory",
    "UnknownVoiceEngineError",
    "build_stt",
    "build_tts",
    "register_stt",
    "register_tts",
    "registered_stt",
    "registered_tts",
]
