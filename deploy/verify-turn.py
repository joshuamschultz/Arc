#!/usr/bin/env python3
"""Prove a deployed agent actually answers — the only check that means anything.

A fleet can be `active`, return `/health` 200, log no tracebacks and pass every
preflight while every single message dies. That shipped twice. The difference
between "the process is up" and "a person gets an answer" is the whole gap, and
this is the smallest thing that closes it from outside the code.

Usage::

    python3 deploy/verify-turn.py                    # every agent in the roster
    python3 deploy/verify-turn.py josh_agent         # one agent

Exits non-zero if any agent fails to answer, so it can gate a deploy script.
Reads ``VIEWER_TOKEN`` / ``OPERATOR_TOKEN`` from ``<arc home>/config/arc.env``.

Needs ``websockets``; run it with the deployment's interpreter
(``~/arc/.venv/bin/python``), not the system one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

#: The dashboard's own protocol: authenticate, wait for ready, THEN send. A
#: message frame sent first is silently dropped and the socket closes — which is
#: why every earlier attempt to drive a turn from a script appeared to hang.
PROMPT = "Reply with exactly: ARC-LIVE-OK"
TURN_DEADLINE = float(os.environ.get("ARC_VERIFY_DEADLINE", "180"))


def _arc_home() -> Path:
    return Path(os.environ.get("ARC_CONFIG_DIR", str(Path.home() / ".arc"))).expanduser()


def _token(name: str) -> str:
    env_file = _arc_home() / "config" / "arc.env"
    if value := os.environ.get(name):
        return value
    match = re.search(rf"^{name}=(.*)$", env_file.read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit(f"{name} not found in {env_file}")
    return match.group(1).strip().strip('"').strip("'")


def _base_url() -> str:
    return os.environ.get("ARC_VERIFY_URL", "http://127.0.0.1:8420").rstrip("/")


def roster() -> list[str]:
    """Every agent id the dashboard serves."""
    request = urllib.request.Request(
        f"{_base_url()}/api/team/roster",
        headers={"Authorization": f"Bearer {_token('OPERATOR_TOKEN')}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return [agent["agent_id"] for agent in payload.get("agents", [])]


async def drive_turn(agent_id: str, text: str = PROMPT) -> tuple[bool, str]:
    """Send one message as the dashboard does; return ``(ok, what came back)``."""
    import websockets

    url = f"{_base_url().replace('http', 'ws', 1)}/ws/chat/{agent_id}"
    try:
        async with websockets.connect(url, open_timeout=30) as socket:
            await socket.send(json.dumps({"token": _token("VIEWER_TOKEN")}))
            ready = json.loads(await asyncio.wait_for(socket.recv(), 30))
            if ready.get("type") != "ready":
                return False, f"no ready frame: {ready}"

            await socket.send(json.dumps({"type": "message", "text": text}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + TURN_DEADLINE
            while loop.time() < deadline:
                remaining = max(5.0, deadline - loop.time())
                frame = json.loads(await asyncio.wait_for(socket.recv(), remaining))
                body = str(frame.get("text", ""))
                if "[agent-error]" in body:
                    return False, body[:300]
                if frame.get("type") == "message" and frame.get("from") == "agent":
                    return True, body[:200]
            return False, f"no reply within {TURN_DEADLINE}s"
    except Exception as exc:  # reason: any failure here is a failed verification
        return False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    agents = sys.argv[1:] or roster()
    if not agents:
        print("no agents in the roster")
        return 1

    failures = 0
    for agent_id in agents:
        ok, detail = await drive_turn(agent_id)
        # A reply that is not the requested text still proves the pipe works;
        # only a failed turn is a failure, so a chatty model does not read as one.
        print(f"{'ok  ' if ok else 'FAIL'} {agent_id}: {detail}")
        failures += not ok

    print(f"\n{len(agents) - failures}/{len(agents)} agents answered")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
