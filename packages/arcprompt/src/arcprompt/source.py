"""PromptSource — the layer-clean prompt lookup seam (COMP-030).

Two layers, one lookup order: **agent override first, then system (stock)**.
Packages below the agent (``arcrun``, ``arcmemory``, ``arcskill``) accept an
optional :class:`PromptSource` — they know arcprompt, never arcagent, and
never an agent path. The agent builds the overlay-aware source and hands it
down through each package's existing seam; a component constructed standalone
falls back to :class:`StockPromptSource`.

:class:`StockPromptSource` is the one production place allowed to call
``arcprompt.load_stock`` outside arcprompt's own stock reader — enforced by
``tests/architecture/test_no_stock_prompt_bypass.py``. Any other package that
wants the effective body of a prompt takes a ``PromptSource`` instead of
calling ``load_stock`` directly, so it never silently skips an agent's
override.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from arcprompt.catalog import load_stock
from arcprompt.errors import PromptMissing
from arcprompt.resolver import PromptResolver
from arcprompt.snapshot import PromptSnapshot


@runtime_checkable
class PromptSource(Protocol):
    """The lookup contract every layer below the agent depends on.

    ``resolve`` always returns the *effective* body: an agent override when
    one exists and is valid, the packaged stock body otherwise. A lower-layer
    package never learns which layer answered, and never sees the agent's
    overlay root or signing key.
    """

    def resolve(self, package: str, name: str) -> str:
        """Return the effective prompt body for ``package``/``name``.

        Raises :class:`~arcprompt.errors.PromptMissing` if no stock prompt is
        packaged for ``package``/``name``.
        """
        ...


class StockPromptSource:
    """The default ``PromptSource``: system prompts only, no overlay layer.

    Used by any component constructed standalone — no agent folder, no
    operator key, e.g. a bare arcrun/arcmemory instance in isolation or in a
    test.
    """

    def resolve(self, package: str, name: str) -> str:
        return load_stock(package, name)


class ResolverPromptSource:
    """Adapt an agent-rooted resolver to :class:`PromptSource`.

    Wraps whichever overlay-aware object the agent already built —
    :class:`~arcprompt.resolver.PromptResolver` (resolves fresh on every
    call) or a frozen :class:`~arcprompt.snapshot.PromptSnapshot` (resolved
    once at run start) — and returns the resolved document's ``body`` instead
    of the document itself, the one shape difference between "resolve to a
    document" (arcprompt's existing contract) and this seam's "resolve to a
    body" (what a lower-layer consumer wants).
    """

    def __init__(self, resolver: PromptResolver | PromptSnapshot) -> None:
        self._resolver = resolver

    def resolve(self, package: str, name: str) -> str:
        if isinstance(self._resolver, PromptSnapshot):
            if (package, name) not in self._resolver:
                raise PromptMissing(package, name)
            return self._resolver.get(package, name).body
        return self._resolver.resolve(package, name).body


__all__ = ["PromptSource", "ResolverPromptSource", "StockPromptSource"]
