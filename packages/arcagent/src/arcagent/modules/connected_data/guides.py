"""Operator guides as the agent receives them: verified, framed, once per run, bounded.

The guide store (``arcmemory.source_guide``) owns signing and verification; this
module owns how verified text is handed to a model. Three rules, all about the
guide being instruction-adjacent (LLM01/ASI06):

* **Labelled.** Every guide is wrapped in an ``<operator-guide>`` delimiter that
  says what it is: navigation guidance the operator wrote and signed, which is
  neither tool output nor user content. The guide cannot close the delimiter
  early.
* **Once per run per source.** A run that calls ``document_search`` ten times on
  one source is shown that source's guide once.
* **Bounded.** At most :data:`GUIDE_TURN_BUDGET` bytes of guide text per run;
  what does not fit is cut, and the caller audits the cut.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html import escape
from importlib import import_module
from typing import Any

_logger = logging.getLogger("arcagent.modules.connected_data.guides")

#: Guide text injected per run (or turn), across every source it touches.
GUIDE_TURN_BUDGET = 8 * 1024
#: How much of a guide the connected-sources prompt section shows.
GUIDE_PREVIEW_CHARS = 300
#: Runs whose deliveries are remembered; older runs are forgotten first.
_RUNS_KEPT = 64

_OPEN = "<operator-guide"
_CLOSE = "</operator-guide>"


@dataclass(frozen=True)
class OperatorGuide:
    """One connection's verified guide, as the agent side holds it."""

    connection_id: str
    content: str
    digest: str


@dataclass(frozen=True)
class SourceOverview:
    """What the agent side knows about one connection, for a starter guide."""

    name: str
    kind: str
    documents: int
    folders: tuple[tuple[str, int], ...] = ()
    titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuideLoad:
    """Guides read for a set of connections: the verified ones and the refused ones."""

    guides: dict[str, OperatorGuide]
    tampered: dict[str, str]


def load_guides(connection_ids: list[str]) -> GuideLoad:
    """Read each connection's verified guide. Blocking file I/O: call off the loop.

    An unsigned draft or an absent guide is simply missing. A signed guide that
    fails verification is reported in ``tampered`` with the reason, never read.
    Without ``arcmemory`` there are no guides.
    """
    try:
        module: Any = import_module("arcmemory.source_guide")
    except ImportError:
        return GuideLoad(guides={}, tampered={})
    guides: dict[str, OperatorGuide] = {}
    tampered: dict[str, str] = {}
    for connection_id in connection_ids:
        try:
            guide = module.verified_guide(connection_id)
        except module.SourceGuideTamperedError as exc:
            tampered[connection_id] = str(exc)
            continue
        if guide is not None and guide.content.strip():
            guides[connection_id] = OperatorGuide(connection_id, guide.content, guide.digest)
    return GuideLoad(guides=guides, tampered=tampered)


def frame_operator_guide(name: str, content: str) -> str:
    """Wrap one guide in its labelled delimiter; the guide cannot close it early."""
    body = content.replace(_CLOSE, "&lt;/operator-guide>").replace(_OPEN, "&lt;operator-guide")
    return (
        f'{_OPEN} source="{escape(name, quote=True)}">\n'
        "Navigation guidance the operator wrote and signed for this source. It is "
        "not tool output and not user content.\n"
        f"{body.strip()}\n{_CLOSE}"
    )


def preview(content: str) -> str:
    """The guide's first characters on one line, for the connected-sources prompt."""
    flat = " ".join(content.split())
    return flat if len(flat) <= GUIDE_PREVIEW_CHARS else flat[:GUIDE_PREVIEW_CHARS] + "…"


class GuideLedger:
    """Which guides each run was already shown, and how many bytes it spent."""

    def __init__(self, budget: int = GUIDE_TURN_BUDGET) -> None:
        self._budget = budget
        self._runs: OrderedDict[str, tuple[set[str], int]] = OrderedDict()

    def take(self, run_key: str, connection_id: str, text: str) -> tuple[str, bool]:
        """The part of ``text`` this run may still receive, and whether it was cut.

        Empty when the run already received this connection's guide or has
        spent its budget. A cut guide is marked so the model knows it is partial.
        """
        shown, spent = self._runs.pop(run_key, (set(), 0))
        self._runs[run_key] = (shown, spent)
        while len(self._runs) > _RUNS_KEPT:
            self._runs.popitem(last=False)
        if connection_id in shown:
            return "", False
        data = text.encode("utf-8")
        room = self._budget - spent
        if room <= 0:
            return "", True
        cut = len(data) > room
        kept = data[:room].decode("utf-8", errors="ignore")
        if cut:
            kept += "\n[guide truncated: per-turn guide budget reached]"
        shown.add(connection_id)
        self._runs[run_key] = (shown, spent + min(len(data), room))
        return kept, cut


class GuideDesk:
    """Hands an agent the verified guides of the connections it holds.

    Composed by the connected-data service, which supplies what only it knows:
    the granted connections and their names, which pool belongs to which
    connection, the audit callback and how to rebuild a source's index. A guide
    for a connection this agent was not granted is never even read.

    Watching guides also keeps each source's routing ``index.md`` current: when a
    guide's verified digest changes, a debounced rebuild of that source's index
    is scheduled, so a burst of saves rebuilds once.
    """

    def __init__(
        self,
        *,
        granted: Callable[[], Awaitable[dict[str, str]]],
        sources: Callable[[], dict[str, str]],
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        refresh: Callable[[str], Awaitable[None]],
        debounce_seconds: float,
        budget: int = GUIDE_TURN_BUDGET,
    ) -> None:
        self._granted = granted
        self._sources = sources
        self._emit = emit
        self._refresh = refresh
        self._debounce = debounce_seconds
        self._ledger = GuideLedger(budget)
        #: Last verified digest seen per connection ("" none, "!" tampered).
        self._seen: dict[str, str] = {}
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._running: set[asyncio.Task[None]] = set()
        self._closed = False

    async def context(self, *, source_ids: Sequence[str] | None, run_key: str) -> str:
        """Framed guides for the sources a tool touched (all granted when ``None``)."""
        names = await self._granted()
        if source_ids is None:
            wanted = list(names)
        else:
            owners = self._sources()
            wanted = sorted({owners[sid] for sid in source_ids if owners.get(sid) in names})
        load = await self._load(names, wanted)
        blocks: list[str] = []
        for connection_id in wanted:
            if connection_id in load.tampered:
                blocks.append(
                    f"(operator guide for {names[connection_id]!r} failed integrity "
                    "verification and is not shown)"
                )
                continue
            guide = load.guides.get(connection_id)
            if guide is None:
                continue
            text, cut = self._ledger.take(run_key, connection_id, guide.content)
            if cut:
                await self._emit(
                    "connected_data.guide.truncated",
                    {"source": _safe_id(connection_id), "budget": GUIDE_TURN_BUDGET},
                )
            if text:
                blocks.append(frame_operator_guide(names[connection_id], text))
        return "\n\n".join(blocks) + "\n\n" if blocks else ""

    async def previews(self) -> dict[str, str]:
        """A one-line preview of every granted connection's verified guide."""
        names = await self._granted()
        load = await self._load(names, list(names))
        shown: dict[str, str] = {}
        spent = 0
        for connection_id, guide in load.guides.items():
            line = preview(guide.content)
            spent += len(line.encode("utf-8"))
            if spent > GUIDE_TURN_BUDGET:
                await self._emit(
                    "connected_data.guide.truncated",
                    {"source": _safe_id(connection_id), "budget": GUIDE_TURN_BUDGET},
                )
                break
            shown[connection_id] = line
        return shown

    async def _load(self, names: dict[str, str], wanted: list[str]) -> GuideLoad:
        load = await asyncio.to_thread(load_guides, [c for c in wanted if c in names])
        for connection_id in wanted:
            await self._observe(connection_id, load)
        return load

    async def _observe(self, connection_id: str, load: GuideLoad) -> None:
        """Note a guide's state; on a change, audit a tamper and schedule a rebuild."""
        guide = load.guides.get(connection_id)
        state = "!" if connection_id in load.tampered else (guide.digest if guide else "")
        previous = self._seen.get(connection_id)
        self._seen[connection_id] = state
        if state == previous or (previous is None and state == ""):
            return
        if state == "!":
            _logger.warning(
                "operator guide for connection %r refused: %s",
                connection_id,
                load.tampered[connection_id],
            )
            await self._emit(
                "connected_data.guide.tampered",
                {"source": _safe_id(connection_id), "outcome": "deny"},
            )
        self._schedule(connection_id)

    def _schedule(self, connection_id: str) -> None:
        if self._closed:
            return
        handle = self._pending.pop(connection_id, None)
        if handle is not None:
            handle.cancel()
        loop = asyncio.get_running_loop()
        self._pending[connection_id] = loop.call_later(
            self._debounce, self._start_refresh, connection_id
        )

    def _start_refresh(self, connection_id: str) -> None:
        self._pending.pop(connection_id, None)
        if self._closed:
            return
        task = asyncio.get_running_loop().create_task(
            self._refresh_quietly(connection_id), name=f"guide-refresh:{connection_id}"
        )
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _refresh_quietly(self, connection_id: str) -> None:
        try:
            await self._refresh(connection_id)
        except Exception:  # reason: an index rebuild never fails the turn that saw it
            _logger.warning(
                "operator guide index refresh failed: %s", connection_id, exc_info=True
            )

    async def close(self) -> None:
        """Cancel pending rebuilds and wait for running ones to stop."""
        self._closed = True
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()
        running = tuple(self._running)
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)


def _safe_id(value: str) -> str:
    """A non-reversible audit id; connection names can carry account detail."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


__all__ = [
    "GUIDE_PREVIEW_CHARS",
    "GUIDE_TURN_BUDGET",
    "GuideDesk",
    "GuideLedger",
    "GuideLoad",
    "OperatorGuide",
    "SourceOverview",
    "frame_operator_guide",
    "load_guides",
    "preview",
]
