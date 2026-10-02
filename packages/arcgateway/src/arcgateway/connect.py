"""Bind a Telegram bot to one agent — the shared core behind every connect surface.

Both ``arc gateway connect-telegram`` (arccli) and the arcui settings panel call
:func:`connect_telegram`. It stores the bot token in the env file the gateway reads
(0600, never the config) under a per-agent var, and writes a per-agent
``[platforms.<slug>_telegram]`` block to ``gateway.toml`` with ``platform = "telegram"``
so the fleet can run one bot per agent (see ``adapters/registry.py``). The token is a
credential: callers must never echo, log, or route it through an agent chat/LLM.

Lives in arcgateway (not arccli) because it mutates gateway config, and arcui — which
also needs it — depends on arcgateway, never on arccli.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import arcagent

if TYPE_CHECKING:
    from arcgateway.adapters.voice.config import WakeConfig

# Telegram bot tokens: "<bot_id>:<secret>" — digits, colon, ~35 url-safe chars.
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
# Platform block names must satisfy the adapter-name guard (^[a-z][a-z0-9_]{0,31}$).
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def connect_telegram(
    *,
    agent_slug: str,
    agent_did: str,
    token: str,
    user_id: int,
    gateway_config: Path,
    env_file: Path,
) -> dict[str, str]:
    """Wire a Telegram bot to ``agent_did``. Returns ``{block, token_env, agent_did}``.

    Stores ``token`` in ``env_file`` under a per-agent var (0600) and adds/replaces a
    ``[platforms.<block>]`` telegram block in ``gateway_config`` bound to ``agent_did``
    and allowlisted to ``user_id``. Raises ``ValueError`` on a malformed token or DID —
    nothing is written in that case.
    """
    if not _TOKEN_RE.match(token.strip()):
        raise ValueError(
            "that does not look like a Telegram bot token (expected '<digits>:<letters>' "
            "from @BotFather). Nothing was written."
        )
    if not agent_did.strip():
        raise ValueError("no agent DID to bind the bot to. Nothing was written.")
    slug = _SLUG_RE.sub("_", agent_slug.lower()).strip("_") or "agent"
    block = f"{slug}_telegram"[:32]
    token_env = f"TELEGRAM_BOT_TOKEN_{slug.upper()}"

    _upsert_env(env_file, token_env, token.strip())
    _write_gateway_block(gateway_config, block, token_env, agent_did.strip(), user_id)
    return {"block": block, "token_env": token_env, "agent_did": agent_did.strip()}


def _upsert_env(env_file: Path, key: str, value: str) -> None:
    """Insert or replace ``KEY=value`` in the env file, keeping it owner-only (0600)."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    env_file.chmod(0o600)


def _write_gateway_block(
    gateway_config: Path, block: str, token_env: str, agent_did: str, user_id: int
) -> None:
    """Add/replace the agent's telegram block in gateway.toml + ensure pairing is on."""
    data: dict[str, Any] = {}
    if gateway_config.is_file():
        data = tomllib.loads(gateway_config.read_text(encoding="utf-8"))
    platforms = data.setdefault("platforms", {})
    platforms[block] = {
        "enabled": True,
        "platform": "telegram",
        "token_env": token_env,
        "agent_did": agent_did,
        "allowed_user_ids": [user_id],
    }
    data.setdefault("security", {})["require_pairing"] = True
    gateway_config.parent.mkdir(parents=True, exist_ok=True)
    gateway_config.write_text(arcagent.dumps_toml(data), encoding="utf-8")


def connect_voice(
    *,
    agent_did: str,
    gateway_config: Path,
    env_file: Path,
    token: str | None = None,
    model_dir: str = "~/voicedev/kokoro",
    blend: str = "af_jessica:0.6,af_nicole:0.4",
    speed: float = 1.12,
    chat_id: str = "olivia",
    port: int = 8790,
    wake_words: list[str] | None = None,
) -> dict[str, str]:
    """Wire the desk voice channel to ``agent_did``. Returns ``{token_env, agent_did, token}``.

    Generates (or accepts) a pairing token, stores it in ``env_file`` (0600) under
    ``ARC_VOICE_TOKEN``, and writes a ``[platforms.voice]`` block bound to the agent
    with a Kokoro cascade engine (blend + speed are config, tunable later). Models
    are per-box under ``model_dir`` (never vendored). Shared by the CLI and arcui.

    ``wake_words`` (validated, 1-3) is what the mic box listens for; omitted, an
    earlier setting is kept, else the channel name. ``listening`` is kept if set.
    """
    import secrets

    if not agent_did.strip():
        raise ValueError("no agent DID to bind voice to. Nothing was written.")
    if ":" in chat_id:
        raise ValueError("chat_id must not contain ':' (collides with the reply address).")
    token = (token or secrets.token_hex(32)).strip()
    token_env = "ARC_VOICE_TOKEN"  # noqa: S105 - env var NAME, not a secret value
    existing = gateway_config.read_text(encoding="utf-8") if gateway_config.exists() else ""
    data: dict[str, Any] = tomllib.loads(existing) if existing else {}
    platforms = data.setdefault("platforms", {})
    previous = platforms.get("voice", {}) if isinstance(platforms.get("voice"), dict) else {}
    wake = _wake_block(previous.get("wake"), wake_words, default_word=chat_id)  # validates first
    _upsert_env(env_file, token_env, token)

    md = str(Path(model_dir).expanduser())
    platforms["voice"] = {
        "enabled": True,
        "listening": bool(previous.get("listening", True)),
        "wake": wake,
        "agent_did": agent_did.strip(),
        "host": "0.0.0.0",  # noqa: S104 - LAN reachability for the desk mic client
        "port": port,
        "token_env": token_env,
        "chat_id": chat_id,
        "engine": {
            "tts": "kokoro",
            "stt": "whisper",
            "kokoro": {
                "model_path": f"{md}/kokoro-v1.0.onnx",
                "voices_path": f"{md}/voices-v1.0.bin",
                "blend": blend,
                "speed": speed,
            },
            "whisper": {"model": "tiny", "device": "cpu", "compute_type": "int8"},
        },
    }
    data.setdefault("security", {})["require_pairing"] = True
    gateway_config.parent.mkdir(parents=True, exist_ok=True)
    gateway_config.write_text(arcagent.dumps_toml(data), encoding="utf-8")
    return {"token_env": token_env, "agent_did": agent_did.strip(), "token": token}


def _wake_block(previous: object, words: list[str] | None, *, default_word: str) -> dict[str, Any]:
    """The ``[platforms.voice.wake]`` table: validated words, other fields preserved."""
    from arcgateway.adapters.voice.config import WakeConfig  # voice-only; folder stays deletable

    base = dict(previous) if isinstance(previous, dict) else {}
    if words is not None:
        base["words"] = words
    elif not base.get("words"):
        base["words"] = [default_word.lower()] if _is_valid_word(default_word.lower()) else []
    try:
        return WakeConfig.model_validate(base).model_dump()
    except ValueError as exc:
        raise ValueError(_first_error(exc)) from exc


def _is_valid_word(word: str) -> bool:
    from arcgateway.adapters.voice.config import WakeConfig  # voice-only; folder stays deletable

    try:
        WakeConfig(words=[word])
    except ValueError:
        return False
    return True


def _voice_table(data: dict[str, Any]) -> dict[str, Any]:
    voice = data.get("platforms", {}).get("voice")
    if not isinstance(voice, dict) or not voice.get("enabled"):
        raise ValueError("voice is not connected. Run connect-voice first.")
    return voice


def set_voice_listening(*, gateway_config: Path, on: bool) -> None:
    """Persist the listening ON/OFF choice so a restart keeps it."""
    existing = gateway_config.read_text(encoding="utf-8") if gateway_config.exists() else ""
    data: dict[str, Any] = tomllib.loads(existing) if existing else {}
    _voice_table(data)["listening"] = on
    gateway_config.write_text(arcagent.dumps_toml(data), encoding="utf-8")


def set_voice_wake(
    *, gateway_config: Path, words: list[str], mode: str | None = None, match: str | None = None
) -> WakeConfig:
    """Validate and persist the typed wake word(s). Returns the stored config."""
    from arcgateway.adapters.voice.config import WakeConfig  # voice-only; folder stays deletable

    existing = gateway_config.read_text(encoding="utf-8") if gateway_config.exists() else ""
    data: dict[str, Any] = tomllib.loads(existing) if existing else {}
    voice = _voice_table(data)
    block = dict(voice.get("wake") or {})
    block["words"] = words
    if mode is not None:
        block["mode"] = mode
    if match is not None:
        block["match"] = match
    try:
        wake = WakeConfig.model_validate(block)
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        raise ValueError(_first_error(exc)) from exc
    voice["wake"] = wake.model_dump()
    gateway_config.write_text(arcagent.dumps_toml(data), encoding="utf-8")
    return wake


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        first = errors()[0]
        return str(first.get("msg", "invalid wake word")).removeprefix("Value error, ")
    return str(exc)


__all__ = ["connect_telegram", "connect_voice", "set_voice_listening", "set_voice_wake"]
