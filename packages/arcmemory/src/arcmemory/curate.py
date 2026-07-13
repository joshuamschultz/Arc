"""Input curation — feed distillation the session conversation, not the machinery.

Distillation (facts, insights, day summaries) should learn from the CONVERSATION —
the user's turns and the agent's responses — NOT from the high-volume tool-call
frames the agent emits while working (argument echoes, results, retries, traces) or
any other operational/runtime plumbing. Passing only the conversation to the LLM is:

* **fewer tokens** — a session is a handful of turns; tool frames are the bulk;
* **less LLM reliance** — we don't ask the model to "ignore" noise, it never sees it;
* **more deterministic** — the model literally cannot mint a fact/insight about the
  agent's own tool-running or messaging mechanics, because those events are removed
  before the call.

This filter is **pure and deterministic** — a kind-based keep list, no LLM or
embedding call. It preserves order and ``event_id`` so downstream citations stay
intact, and runs upstream of chunking so it also shrinks the token budget. Toggle it
off (``cfg.curate_input=False``) for the identity function.
"""

from __future__ import annotations

from arcmemory.config import MemoryConfig
from arcmemory.types import Event


def curate_for_distillation(events: list[Event], cfg: MemoryConfig) -> list[Event]:
    """Return only the session-conversation events worth distilling.

    Keeps events whose ``kind`` is in ``cfg.curate_conversation_kinds`` (default the
    user's turns + the agent's responses) and drops ``tool`` frames and every other
    operational kind. Order and ``event_id`` are preserved.
    """
    if not cfg.curate_input:
        return events
    keep = cfg.curate_conversation_kinds
    return [event for event in events if event.kind in keep]


__all__ = ["curate_for_distillation"]
