"""Shared helper for the messaging tool surface.

The LLM-callable messaging tools live as ``@tool`` decorators in
:mod:`arcagent.modules.messaging.capabilities`. This module holds the one
backend helper they share.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("arcagent.messaging.tools")

# Messenger uses this collection name for streams — must match
# arcteam.messenger.STREAMS_COLLECTION. Kept local to avoid a hard
# import dependency on arcteam from the tools surface.
_STREAMS_COLLECTION = "streams"


async def _stream_end_byte_pos(svc: Any, stream: str) -> int:
    """Best-effort fetch of the stream end byte offset.

    Falls back to ``0`` if the backend doesn't expose the helper
    (older backend implementations). Never raises — cursor seek is an
    optimization, not a correctness guarantee.
    """
    backend = getattr(svc, "_backend", None)
    get_end = getattr(backend, "get_stream_end_byte_pos", None)
    if get_end is None:
        return 0
    try:
        return int(await get_end(_STREAMS_COLLECTION, stream))
    except Exception:  # reason: fail-open — log + continue
        _logger.debug("stream end byte_pos fetch failed; using 0", exc_info=True)
        return 0
