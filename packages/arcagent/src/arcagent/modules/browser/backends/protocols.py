"""Backend seam for the browser module.

The 18 browser ``@tool`` functions all drive the page through a single
object exposing ``send()`` and ``url`` — the Chrome DevTools Protocol
transport. That object is a :class:`BrowserSession`.

Where that session comes from — a locally launched Chrome, a remote CDP
endpoint, or a managed service like Browserbase — is the concern of a
:class:`BrowserBackend`. A backend ``open()``s a ready-to-drive session
and ``close()``s it (releasing any remote resources).

Any adapter implementing these Protocols is pluggable without inheriting
a base class — pure duck-typing, exactly like the web module's provider
seam. Adding a new browser service is one new file plus a name in
``[modules.browser.config] provider``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrowserSession(Protocol):
    """A live, page-level CDP session that browser tools drive.

    :class:`~arcagent.modules.browser.cdp_client.CDPClientManager`
    satisfies this Protocol as-is — the tools never learn which backend
    produced the session.
    """

    @property
    def url(self) -> str:
        """The CDP WebSocket URL this session is connected to."""
        ...

    async def send(
        self, domain: str, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a CDP command and return its ``result`` dict."""
        ...


@runtime_checkable
class BrowserBackend(Protocol):
    """A source of :class:`BrowserSession` objects.

    ``open()`` connects (launching or attaching as appropriate) and
    returns a session ready for Page/DOM/Accessibility commands.
    ``close()`` disconnects and releases any remote resources — it must
    be safe to call even if ``open()`` never ran or already failed.

    ``name`` is a stable identifier used in audit events and logs.
    """

    name: str

    async def open(self) -> BrowserSession:
        """Connect and return a ready-to-drive page-level session."""
        ...

    async def close(self) -> None:
        """Disconnect the session and release any remote resources."""
        ...


__all__ = ["BrowserBackend", "BrowserSession"]
