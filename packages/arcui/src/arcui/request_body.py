"""Size-capped request body reading — the one helper every capped route uses.

A route that reads ``await request.body()`` or ``await request.json()`` buffers
whatever the caller sends. Routes reachable by apps or the network read through
:func:`read_capped_body` instead, so an oversized body is refused before it is
buffered or parsed (OWASP LLM10 unbounded consumption).
"""

from __future__ import annotations

from starlette.requests import Request


async def read_capped_body(request: Request, cap: int) -> bytes | None:
    """The request body, or ``None`` once it is over ``cap`` bytes.

    The declared ``Content-Length`` is checked first, then the stream itself is
    counted as it arrives, so a lying or absent ``Content-Length`` cannot slip
    a larger body past the cap. Nothing past the cap is buffered.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        return None
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > cap:
            return None
    return bytes(received)


__all__ = ["read_capped_body"]
