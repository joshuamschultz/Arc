"""The connector-authoring contract, codified as a runnable checker (COMP-012).

Every ``SourceAdapter`` must honour an unwritten contract so the next connector
inherits the reliability fixes instead of re-earning the bugs. This module turns
that contract into :func:`assert_connector_contract`, an async helper that drives
an adapter through its lifecycle against a fresh, un-credentialed connection and
raises :class:`AssertionError` naming the clause a connector violates.

The four clauses (see ``docs/concepts/connector-authoring.md`` for the full guide):

1. **Monotonic revision.** Every synced object carries a non-decreasing
   ``metadata["revision"]``; ArcMemory refuses an update whose revision is not
   greater than the stored one, so a decreasing revision silently stops
   re-indexing.
2. **Lazy client re-creation after ``close_source``.** A call after
   ``close_source`` still works — the adapter rebuilds its client rather than
   holding a dead handle.
3. **Typed failures.** A refusal surfaces as a typed, coded project error
   (:class:`~arcagent.extension.source.SourceError`, or an
   :class:`~arcagent.core.errors.ArcAgentError` such as ``ExtensionError`` leaking
   up from the attachment layer), never a bare ``Exception`` an orchestrator
   cannot classify or back off from.
4. **Tool refusals are returned, not raised.** An attachment reports a tool-level
   refusal as ``ToolResult(outcome=ERROR)`` rather than raising — checked at the
   ``ExtensionAttachment`` seam, documented here and in the guide (a
   ``SourceAdapter`` has no tool-call surface, so this clause is not asserted by
   this helper).

The clause governs *how* a failure surfaces, not that every call succeeds: a
connector that fails only via a typed error is conformant. That is why a real
CLI-backed bundle passes offline — with no vendor binary present its lifecycle
raises only typed errors — while a fake that raises a bare ``RuntimeError`` is
flagged.
"""

from __future__ import annotations

from typing import Any

from arcagent.core.errors import ArcAgentError
from arcagent.extension.source import (
    InspectSource,
    SourceAdapter,
    SourceError,
    SyncSource,
    SyncSourcePage,
)

#: Failures a conformant connector is allowed to raise: a typed source refusal, or
#: the agent error hierarchy an attachment layer surfaces. Anything else is bare.
_TYPED_FAILURES = (SourceError, ArcAgentError)


async def assert_connector_contract(adapter: SourceAdapter, *, connection_id: str) -> None:
    """Drive ``adapter`` through its lifecycle and assert the authoring contract.

    Raises:
        AssertionError: naming the violated clause — a bare-exception failure
            (clause 3), a non-monotonic revision (clause 1), or a dead client
            after ``close_source`` (clause 2).
    """
    request = SyncSource(connection_id=connection_id)
    try:
        await adapter.inspect_source(InspectSource(connection_id=connection_id))
        page = await adapter.sync_source(request)
    except _TYPED_FAILURES:
        # A typed refusal is conformant: the clause governs HOW a failure surfaces,
        # not that a fresh, un-credentialed connection must succeed.
        return
    except AssertionError:
        raise
    except Exception as exc:  # classifying an untyped failure is the whole point
        raise AssertionError(
            "typed-failure clause: a connector must surface a refusal as a typed "
            f"SourceError, not a bare {type(exc).__name__} ({exc!r})"
        ) from exc

    _assert_monotonic_revision(page)
    await _assert_lazy_reopen(adapter, request)


def _assert_monotonic_revision(page: SyncSourcePage) -> None:
    """Every object in a page carries a non-decreasing ``metadata['revision']``."""
    previous: Any = None
    for obj in page.objects:
        revision = obj.metadata.get("revision")
        if revision is None:
            raise AssertionError(
                f"monotonic-revision clause: object {obj.object_id!r} carries no "
                "metadata['revision']; ArcMemory needs one to re-index a changed object"
            )
        if previous is not None and revision < previous:
            raise AssertionError(
                f"monotonic-revision clause: revision {revision!r} follows {previous!r} in "
                "one page; a decreasing revision makes ArcMemory silently stop re-indexing"
            )
        previous = revision


async def _assert_lazy_reopen(adapter: SourceAdapter, request: SyncSource) -> None:
    """A call after ``close_source`` must rebuild the client and still work."""
    await adapter.close_source()
    try:
        await adapter.sync_source(request)
    except AssertionError:
        raise
    except Exception as exc:  # any raise after close is itself the violation
        raise AssertionError(
            "lazy-reopen clause: a call after close_source must rebuild the client and "
            f"succeed, but it raised {type(exc).__name__} ({exc!r})"
        ) from exc


__all__ = ["assert_connector_contract"]
