"""arc-agent-worker — Per-session subprocess entry point for federal-tier isolation.

This module is the JSON-lines IPC worker spawned by SubprocessExecutor.
Each subprocess represents one isolated session with:
  - Its own asyncio event loop
  - Its own httpx connection pool (no shared cross-session connections)
  - Its own ToolRegistry
  - Its own audit chain (audit events are not visible to parent or siblings)

Protocol (stdin/stdout JSON-lines):
  Input  (stdin):  One InboundEvent JSON object per line, UTF-8.
  Output (stdout): One Delta JSON object per line per event, ending with
                   a Delta(kind="done", is_final=True). UTF-8.
  Errors (stderr): Structured log lines; never used for IPC.

Lifecycle:
  1. Parent spawns: ``arc-agent-worker --did <agent_did>``
  2. Worker reads lines from stdin in a loop.
  3. For each line: parse InboundEvent, run agent, write Delta lines.
  4. EOF on stdin → worker exits 0.
  5. Malformed JSON → log warning on stderr, write error Delta, continue.

arcagent wiring:
  _run_event() calls _run_with_arcagent() which:
    1. Locates arcagent.toml in the standard locations.
    2. Instantiates ArcAgent with the config.
    3. Opens the event's session and drives the one streaming entry,
       ``collect(agent.run(message, session=...))``, wrapping the final
       result in Delta objects.

  If arcagent is not installed or no config file is found, the function
  falls back to the echo stub and logs a WARNING so operators can diagnose
  the issue without the subprocess crashing.

Resource limits:
  Applied by SubprocessExecutor via preexec_fn before this process starts.
  This module does NOT call setrlimit — that is the parent's responsibility.
  See arcgateway.executor.SubprocessExecutor for the resource-limit contract.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from arcrun import collect

_logger = logging.getLogger("arc.agent.worker")

# Config search locations for arc-agent-worker (same order as the arc CLI).
# The subprocess inherits the parent's cwd, so relative paths work. This is
# the LEGACY fallback used only when the caller does not supply --team-root;
# it has no way to know which agent's config it will find, which is exactly
# why the --did verification below exists.
_CONFIG_SEARCH_PATHS: list[Path] = [
    Path("arcagent.toml"),
    Path("~/.arc/agent.toml").expanduser(),
    Path("~/.arcagent/arcagent.toml").expanduser(),
]


# ---------------------------------------------------------------------------
# DID-indexed config resolution (task 26)
# ---------------------------------------------------------------------------


def _load_did_index(team_root: Path) -> dict[str, Path]:
    """Build a ``did -> agent_dir`` map from every arcagent.toml under team_root.

    Mirrors ``arcgateway.bootstrap._load_did_index`` exactly (same discovery
    signal: presence of ``arcagent.toml`` one level under team_root). Not
    imported from arcgateway — arccli has no dependency on it; the gateway
    spawns this worker as a subprocess, never the reverse.
    """
    if not team_root.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # pragma: no cover — Python <3.11 fallback
        import tomli as tomllib  # type: ignore[no-redef]  # reason: Python <3.11 fallback — tomli is the same API as stdlib tomllib

    index: dict[str, Path] = {}
    for toml_path in sorted(team_root.glob("*/arcagent.toml")):
        agent_dir = toml_path.parent
        if not agent_dir.is_dir():
            continue
        try:
            cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        identity = cfg.get("identity", {}) if isinstance(cfg.get("identity"), dict) else {}
        did = identity.get("did")
        if isinstance(did, str) and did:
            index[did] = agent_dir
    return index


def _resolve_config_path(agent_did: str, team_root: Path | None) -> Path | None:
    """Resolve the arcagent.toml path this worker should load.

    When ``team_root`` is supplied (the embedded/federal production path —
    threaded from SubprocessExecutor's ``--team-root`` argument), resolution
    is DID-indexed: only a config whose ``[identity].did`` equals ``agent_did``
    is eligible, so an unrelated agent's config can never be selected.
    Without ``team_root`` (legacy/test invocation), falls back to the fixed
    search paths — the config found there is unrelated to ``agent_did`` by
    construction, which is exactly why callers of this function verify the
    loaded config's own DID afterward (see ``_run_with_arcagent``).
    """
    if team_root is not None:
        agent_dir = _load_did_index(team_root).get(agent_did)
        return agent_dir / "arcagent.toml" if agent_dir is not None else None
    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            return candidate
    return None


def _identity_mismatch_deltas(
    requested_did: str, loaded_did: str, config_path: Path
) -> list[dict[str, object]]:
    """Fail-closed response for a resolved config whose DID doesn't match.

    Emits a structured audit log line to stderr (the worker's only IPC-safe
    audit channel — stdout is reserved for Delta output; the parent process
    inherits this worker's stderr into the gateway's log stream) and returns
    a single error Delta rather than any content from the mismatched agent.
    """
    _logger.error(
        "AUDIT event=worker.did_mismatch data=%s",
        {
            "requested_did": requested_did,
            "loaded_did": loaded_did,
            "config_path": str(config_path),
        },
        extra={
            "audit_event": "worker.did_mismatch",
            "audit_data": {
                "requested_did": requested_did,
                "loaded_did": loaded_did,
                "config_path": str(config_path),
            },
        },
    )
    return [
        {
            "kind": "done",
            "content": (
                f"[worker-error] identity mismatch: requested did:arc:agent="
                f"{requested_did!r} but resolved config at {config_path} declares "
                f"{loaded_did!r} — refusing to run as the wrong agent"
            ),
            "is_final": True,
            "turn_id": "",
        }
    ]


# ---------------------------------------------------------------------------
# ArcAgent adapter
# ---------------------------------------------------------------------------


async def _run_with_arcagent(
    agent_did: str,
    message: str,
    session_key: str,
    team_root: Path | None = None,
) -> list[dict[str, object]]:
    """Run the prompt through ArcAgent and return Delta dicts.

    Resolves config via ``_resolve_config_path`` (DID-indexed when
    ``team_root`` is supplied, else the legacy fixed search paths). If no
    config is found or arcagent is not installed, falls back to the echo
    stub with a logged WARNING. If a config IS found but its own
    ``[identity].did`` does not match ``agent_did``, refuses to run
    (see ``_identity_mismatch_deltas``) rather than silently serving the
    wrong agent's identity — this is the task-26 fix: a multi-agent_did
    gateway must never let one agent's worker execute as another.

    Drives the one streaming entry and collects it to a final result
    (``collect(agent.run(message, session=...))``), wrapping the reply in a
    token Delta followed by the done sentinel.

    Args:
        agent_did: Agent DID passed via --did CLI arg.
        message: User message to process.
        session_key: Session key for turn ID continuity.
        team_root: Directory containing one ``<name>/arcagent.toml`` per
            agent, passed via --team-root. None preserves legacy behaviour.

    Returns:
        List of Delta dicts (JSON-serialisable).
    """
    turn_id = str(uuid.uuid4())

    # Try to import arcagent; fall back to echo stub if not installed.
    try:
        from arcagent.core.agent import ArcAgent
        from arcagent.core.config import load_config as _load_config
    except ImportError:
        _logger.warning(
            "arc-agent-worker: arcagent not installed — falling back to echo stub. "
            "Install arcagent to enable real agent execution in federal-tier workers."
        )
        return _echo_stub(message, session_key)

    config_path = _resolve_config_path(agent_did, team_root)

    if config_path is None:
        _logger.warning(
            "arc-agent-worker: no arcagent.toml found for agent_did=%s (team_root=%s) — "
            "falling back to echo stub.",
            agent_did,
            team_root,
        )
        return _echo_stub(message, session_key)

    _logger.info(
        "arc-agent-worker: loading agent config from %s (agent_did=%s)",
        config_path,
        agent_did,
    )

    try:
        config = _load_config(config_path)
        if config.identity.did != agent_did:
            return _identity_mismatch_deltas(agent_did, config.identity.did, config_path)
        agent = ArcAgent(config, config_path=config_path)
        await agent.startup()

        # One streaming entry, collected to a result, on the event's session.
        session = await agent.session(session_key)
        result = await collect(agent.run(message, session=session))
        content: str = result.content or ""

        _logger.info(
            "arc-agent-worker: agent run complete session=%s content_len=%d",
            session_key,
            len(content),
        )

        return [
            {
                "kind": "token",
                "content": content,
                "is_final": False,
                "turn_id": turn_id,
            },
            {
                "kind": "done",
                "content": "",
                "is_final": True,
                "turn_id": turn_id,
            },
        ]

    except Exception as exc:  # reason: fail-open — log + continue
        _logger.exception(
            "arc-agent-worker: agent error session=%s: %s",
            session_key,
            exc,
        )
        return [
            {
                "kind": "token",
                "content": f"[agent-error] {exc}",
                "is_final": False,
                "turn_id": turn_id,
            },
            {
                "kind": "done",
                "content": "",
                "is_final": True,
                "turn_id": turn_id,
            },
        ]


def _echo_stub(message: str, session_key: str) -> list[dict[str, object]]:
    """Echo stub — returns a minimal Delta sequence.

    Used when arcagent is not installed or no config is found.
    Preserves the IPC protocol so the parent's test assertions pass.
    """
    turn_id = str(uuid.uuid4())
    return [
        {
            "kind": "token",
            "content": (f"[arc-agent-worker echo] Received: {message!r} (session={session_key})"),
            "is_final": False,
            "turn_id": turn_id,
        },
        {
            "kind": "done",
            "content": "",
            "is_final": True,
            "turn_id": turn_id,
        },
    ]


# ---------------------------------------------------------------------------
# Core IPC loop
# ---------------------------------------------------------------------------


async def _run(agent_did: str, team_root: Path | None = None) -> None:
    """Main IPC loop: read InboundEvent lines from stdin, write Delta lines to stdout.

    Runs until EOF on stdin. Each malformed line is logged and skipped;
    the worker does NOT crash on bad input (protocol robustness requirement).

    Args:
        agent_did: The agent DID this worker is configured for. Passed in
            from CLI args so that each subprocess has its own identity anchor.
        team_root: Directory containing one ``<name>/arcagent.toml`` per
            agent, passed via --team-root. Enables DID-indexed config
            resolution; None preserves legacy fixed-path behaviour.
    """
    _logger.info(
        "arc-agent-worker starting: agent_did=%s pid=%d",
        agent_did,
        _getpid(),
    )

    reader = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        sys.stdin.buffer,
    )

    try:
        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                # EOF — clean shutdown
                _logger.info("arc-agent-worker: stdin EOF, exiting cleanly")
                break

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue

            await _handle_line(line, agent_did, team_root)
    finally:
        transport.close()


async def _handle_line(line: str, agent_did: str, team_root: Path | None = None) -> None:
    """Parse one JSON line as InboundEvent and emit Delta lines to stdout.

    On malformed JSON: log a warning and emit a single error Delta so the
    parent knows the line was received but could not be processed.

    Args:
        line: Raw UTF-8 JSON line (no trailing newline).
        agent_did: Forwarded to the agent for identity context.
        team_root: Forwarded to config resolution (see ``_run_with_arcagent``).
    """
    try:
        event_data: dict[str, object] = json.loads(line)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "arc-agent-worker: malformed JSON input (error=%s line=%r)",
            exc,
            line[:120],
        )
        _write_delta(
            {
                "kind": "done",
                "content": f"[worker-error] malformed JSON: {exc}",
                "is_final": True,
                "turn_id": "",
            }
        )
        return

    _logger.debug(
        "arc-agent-worker: processing event session=%s platform=%s",
        event_data.get("session_key"),
        event_data.get("platform"),
    )

    message = str(event_data.get("message", ""))
    session_key = str(event_data.get("session_key", ""))

    try:
        deltas = await _run_with_arcagent(agent_did, message, session_key, team_root)
    except Exception as exc:  # reason: worker must never crash on agent error
        _logger.exception(
            "arc-agent-worker: agent error for session=%s",
            session_key,
        )
        deltas = [
            {
                "kind": "done",
                "content": f"[worker-error] agent raised: {exc}",
                "is_final": True,
                "turn_id": str(session_key),
            }
        ]

    for delta in deltas:
        _write_delta(delta)


def _write_delta(delta: dict[str, object]) -> None:
    """Serialise and write one Delta to stdout as a JSON line.

    Uses sys.stdout directly (not the asyncio stream) because Delta output
    must be synchronous and immediately flushed to guarantee the parent's
    readline() sees the line without buffering delay.

    Args:
        delta: Delta dict conforming to arcgateway.executor.Delta schema.
    """
    sys.stdout.write(json.dumps(delta) + "\n")
    sys.stdout.flush()


def _getpid() -> int:
    """Return current process PID (isolated per-session identity anchor)."""
    import os

    return os.getpid()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """arc-agent-worker CLI entry point.

    Called by arcgateway.executor_subprocess.SubprocessExecutor via:
        asyncio.create_subprocess_exec(
            "arc-agent-worker", "--did", <agent_did>, "--team-root", <team_root>, ...
        )

    Args (from sys.argv):
        --did         Agent DID for this worker subprocess (required).
        --team-root   Directory containing one <name>/arcagent.toml per
                       agent (optional). Enables DID-indexed config
                       resolution so this worker can only ever load the
                       config matching --did (task 26) — without it, config
                       resolution falls back to legacy fixed search paths
                       whose DID is verified against --did after loading,
                       but which cannot be selected per-agent.

    Exits:
        0  on clean EOF / normal termination.
        1  on argument parse failure.
    """
    # Configure stderr-only structured logging so stdout stays clean for IPC.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="arc-agent-worker",
        description=(
            "Per-session subprocess for federal-tier agent isolation. "
            "Reads InboundEvent JSON from stdin; writes Delta JSON to stdout."
        ),
    )
    parser.add_argument(
        "--did",
        required=True,
        metavar="AGENT_DID",
        help="Agent DID for this worker subprocess (e.g. 'did:arc:agent:bot').",
    )
    parser.add_argument(
        "--team-root",
        type=Path,
        default=None,
        metavar="TEAM_ROOT",
        help=(
            "Directory containing one <name>/arcagent.toml per agent. "
            "Enables DID-indexed config resolution so this worker only ever "
            "loads the config matching --did."
        ),
    )
    args = parser.parse_args()

    asyncio.run(_run(args.did, args.team_root))


if __name__ == "__main__":
    main()
