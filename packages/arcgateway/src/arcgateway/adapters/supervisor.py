"""AdapterSupervisor — start remote platform adapters without one sinking the host.

The embedded gateway (``arc ui start``) connects its platform adapters inside
the web server's startup. A connect() that raised there used to abort the whole
process, so a network blip at boot took the dashboard, the gateway and every
agent down with it, and a supervisor restart re-entered the same failure.

Each adapter is now isolated (ASI08 — cascading-failure containment):

- a *transient* failure (network, DNS, timeout — anything with an ``OSError``
  or ``TimeoutError`` in its cause chain) is logged, reported as degraded by
  :meth:`AdapterSupervisor.health`, and retried in the background by the shared
  :func:`reconnect_watcher`;
- any other failure is a misconfiguration (bad token, auth rejected) and still
  raises, so it surfaces at startup rather than hiding behind a live process.

Adapters that recover on their own report their state through an optional
``health()`` method; the supervisor prefers it whenever connect() returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from arcgateway.adapters._reconnect import FailedAdapter, reconnect_watcher
from arcgateway.adapters.base import BasePlatformAdapter

_logger = logging.getLogger("arcgateway.adapters.supervisor")

AdapterHealth = dict[str, str | None]
"""``{"state": ..., "last_error": ...}`` — the shape every status surface reads."""

_CONNECTED: AdapterHealth = {"state": "connected", "last_error": None}


def is_transient_error(exc: BaseException) -> bool:
    """Return True when ``exc`` or anything in its cause chain is a network failure."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (OSError, TimeoutError)):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _adapter_key(adapter: BasePlatformAdapter) -> str:
    """One key per bot: two agents may each run a bot on the same platform."""
    return f"{adapter.name}:{getattr(adapter, 'agent_did', '')}"


class AdapterSupervisor:
    """Connect remote platform adapters in isolation and report their health."""

    def __init__(
        self,
        adapters: Sequence[BasePlatformAdapter],
        *,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        """Hold the adapters; nothing connects until :meth:`start`.

        Args:
            adapters: Remote platform adapters to start.
            poll_interval_seconds: How often the background reconnect checks.
        """
        self._adapters = {_adapter_key(a): a for a in adapters}
        self._failed: dict[str, FailedAdapter] = {}
        self._poll_interval = poll_interval_seconds
        self._watcher: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Connect every adapter; degrade on a transient failure, raise otherwise.

        Raises:
            Exception: The first non-transient connect() failure.
        """
        for key, adapter in self._adapters.items():
            try:
                await adapter.connect()
            except Exception as exc:  # reason: transient → degrade; anything else re-raised
                if not is_transient_error(exc):
                    raise
                _logger.error(
                    "AdapterSupervisor: %s unreachable at startup (%s); running DEGRADED "
                    "and retrying in the background.",
                    key,
                    exc,
                )
                self._failed[key] = FailedAdapter(name=key, last_error=exc)
        if self._failed:
            self._watcher = asyncio.create_task(
                reconnect_watcher(
                    self._failed, self._adapters, poll_interval_seconds=self._poll_interval
                ),
                name="arcgateway:adapter-reconnect",
            )

    def health_of(self, name: str, agent_did: str) -> AdapterHealth | None:
        """Report the bot for platform ``name`` serving ``agent_did``; None if not running."""
        adapter = self._adapters.get(f"{name}:{agent_did}")
        return None if adapter is None else self.health(adapter)

    def health(self, adapter: BasePlatformAdapter) -> AdapterHealth:
        """Report one adapter's state: the supervisor's record, else its own."""
        entry = self._failed.get(_adapter_key(adapter))
        if entry is not None:
            state = "failed" if entry.permanently_failed else "degraded"
            error = str(entry.last_error) if entry.last_error is not None else None
            return {"state": state, "last_error": error}
        reported = getattr(adapter, "health", None)
        if callable(reported):
            result: AdapterHealth = reported()
            return result
        return dict(_CONNECTED)

    async def stop(self) -> None:
        """Cancel the background reconnect; adapters are disconnected by the host."""
        if self._watcher is None:
            return
        self._watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._watcher
        self._watcher = None
