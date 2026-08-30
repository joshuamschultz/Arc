"""Hermes reference worker — the OUT-OF-PROCESS half of the reference harness.

H-040 §9 Posture A (mandatory, all tiers): a foreign harness runs in a subprocess
Arc spawns and owns, so its blast radius is one sandboxed member and it can never
reach ``build_brain`` in the fleet process (the memory-isolation argument, §5.2).
This module is that subprocess entry point.

Protocol (stdin/stdout JSON-lines, mirroring ``arc-agent-worker``):
  Input  (stdin):  one ``{"message": "...", "handle": "..."}`` object per line.
  Output (stdout): one ``MemberOutput``-shaped object per line, ending with a
                   ``{"kind": "done", "is_final": true}`` sentinel.

Hermes is deliberately READ-ONLY and LLM-free: it echoes a bounded acknowledgement
so the seam — enroll → dispatch out-of-process → reply — is proven without a model
call. A real foreign harness would run its own loop here instead.
"""

from __future__ import annotations

import json
import sys

_MAX_ECHO = 400


def _reply_text(message: str, handle: str) -> str:
    body = message.strip().replace("\n", " ")[:_MAX_ECHO]
    return f"[hermes:{handle}] read-only ack: {body}"


def _handle_line(line: str) -> list[dict[str, object]]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        return [{"kind": "error", "text": f"malformed input: {exc}", "is_final": True}]
    message = str(event.get("message", ""))
    handle = str(event.get("handle", "hermes"))
    return [
        {"kind": "text", "text": _reply_text(message, handle), "is_final": False},
        {"kind": "done", "text": "", "is_final": True},
    ]


def main() -> None:
    """Read InboundEnvelope lines from stdin; write MemberOutput lines to stdout."""
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line:
            continue
        for out in _handle_line(line):
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
