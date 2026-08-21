"""COMP-001 — Router fan-out (SPEC-073).

Applies an operator-approved :class:`~arcmemory.types.SourceMapping` and fans
normalized records to one-or-more home sinks. The router carries no
source-shaped logic — that lives in adapters (``adapter.py``) — and no
home-shaped logic either; each sink owns how its home ingests records.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from arcmemory.types import SourceMapping, SourceRecord

Sink = Callable[[str, list[SourceRecord]], Awaitable[None]]


class Router:
    """Fans records to one-or-more home sinks per an approved SourceMapping."""

    def __init__(self, sinks: dict[str, Sink]) -> None:
        self._sinks = sinks

    async def route(
        self, source_id: str, records: list[SourceRecord], mapping: SourceMapping
    ) -> None:
        """Await each sink registered for a home in ``mapping.homes``.

        Unknown or unregistered homes are skipped, never an error. An empty
        ``mapping.homes`` is inert — no sink is called.
        """
        for home in mapping.homes:
            sink = self._sinks.get(home)
            if sink is not None:
                await sink(source_id, records)
