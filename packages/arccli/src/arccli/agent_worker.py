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

arcagent.run() wiring (M1 final integration):
  _run_event() calls _run_with_arcagent() which:
    1. Locates arcagent.toml in the standard locations.
    2. Instantiates ArcAgent with the config.
    3. Calls agent.run(message) and wraps the result in Delta objects.

  If arcagent is not installed or no config file is found, the function
  falls back to the echo stub and logs a WARNING so operators can diagnose
  the issue without the subprocess crashing.

  ArcAgent does not expose a streaming iterator today — run() returns a
  complete result object.  A single token Delta carries the full response.
  True streaming is a future M2 enhancement.

TODO (M2): Replace single-token wrapping with ArcRun event-stream Iterator
    once arcrun exposes an async event generator.

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

_logger = logging.getLogger("arc.agent.worker")

# Config search locations for arc-agent-worker (same order as the arc CLI).
# The subprocess inherits the parent's cwd, so relative paths work.
_CONFIG_SEARCH_PATHS: list[Path] = [
    Path("arcagent.toml"),
    Path("~/.arc/agent.toml").expanduser(),
    Path("~/.arcagent/arcagent.toml").expanduser(),
]


# ---------------------------------------------------------------------------
# ArcAgent adapter
# ---------------------------------------------------------------------------


async def _run_with_arcagent(
    agent_did: str,
    message: str,
    session_key: str,
) -> list[dict[str, object]]:
    """Run the prompt through ArcAgent and return Delta dicts.

    Searches for arcagent.toml in standard locations.  If no config is found
    or arcagent is not installed, falls back to the echo stub with a logged
    WARNING so the operator knows the issue.

    ArcAgent does not expose a streaming iterator — run() returns a complete
    result object.  We wrap the full response in a single token Delta followed
    by the done sentinel.  This is honest: the client receives the full reply
    in one chunk.

    TODO (M2): Replace with streaming once arcrun exposes an async event
    generator from run_async() / AgentHandle.

    Args:
        agent_did: Agent DID passed via --did CLI arg.
        message: User message to process.
        session_key: Session key for turn ID continuity.

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

    # Locate the config file.
    config_path: Path | None = None
    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            config_path = candidate
            break

    if config_path is None:
        _logger.warning(
            "arc-agent-worker: no arcagent.toml found in %s — falling back to echo stub. "
            "Create a config file to enable real agent execution.",
            [str(p) for p in _CONFIG_SEARCH_PATHS],
        )
        return _echo_stub(message, session_key)

    _logger.info(
        "arc-agent-worker: loading agent config from %s (agent_did=%s)",
        config_path,
        agent_did,
    )

    try:
        config = _load_config(config_path)
        agent = ArcAgent(config, config_path=config_path)
        await agent.startup()

        result = await agent.run(message)

        # Extract text from ArcRun result object (has .content attribute).
        content: str = getattr(result, "content", None) or str(result)

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

    except Exception as exc:
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


async def _run(agent_did: str) -> None:
    """Main IPC loop: read InboundEvent lines from stdin, write Delta lines to stdout.

    Runs until EOF on stdin. Each malformed line is logged and skipped;
    the worker does NOT crash on bad input (protocol robustness requirement).

    Args:
        agent_did: The agent DID this worker is configured for. Passed in
            from CLI args so that each subprocess has its own identity anchor.
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

            await _handle_line(line, agent_did)
    finally:
        transport.close()


async def _handle_line(line: str, agent_did: str) -> None:
    """Parse one JSON line as InboundEvent and emit Delta lines to stdout.

    On malformed JSON: log a warning and emit a single error Delta so the
    parent knows the line was received but could not be processed.

    Args:
        line: Raw UTF-8 JSON line (no trailing newline).
        agent_did: Forwarded to the agent for identity context.
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
        deltas = await _run_with_arcagent(agent_did, message, session_key)
    except Exception as exc:  # worker must never crash on agent error
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

    Called by arcgateway.executor.SubprocessExecutor via:
        asyncio.create_subprocess_exec("arc-agent-worker", "--did", <agent_did>, ...)

    Args (from sys.argv):
        --did  Agent DID for this worker subprocess (required).

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
    args = parser.parse_args()

    asyncio.run(_run(args.did))


if __name__ == "__main__":
    main()
