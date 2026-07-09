"""Federal remote-browser policy for the browser module.

Federal tier forbids a locally launched headless Chrome: the browser
must attach to an externally-sandboxed (remote) CDP endpoint. "Local"
means an empty ``connection.cdp_url`` — the module would auto-launch
Chrome as a subprocess. "Remote" means an explicit ``cdp_url`` pointing
at a separately-sandboxed browser.

Enforced on the live launch path (:meth:`BrowserCapability.setup`)
before any Chrome process is started — fail loud, fail closed. This
module contains NO I/O so it is trivially testable.
"""

from __future__ import annotations

from arcagent.modules.browser.config import BrowserConnectionConfig
from arcagent.modules.browser.errors import LocalBrowserNotAllowedError

# Tiers that forbid a locally auto-launched browser (remote CDP required).
_REMOTE_REQUIRED_TIERS: frozenset[str] = frozenset({"federal"})


def enforce_sandbox_policy(tier: str, connection: BrowserConnectionConfig) -> None:
    """Raise if *tier* forbids a local browser and none is remotely attached.

    Args:
        tier:       Deployment tier (``"federal"``, ``"enterprise"``, ``"personal"``).
        connection: The live CDP connection config. An empty ``cdp_url``
            means the module would auto-launch a local Chrome subprocess.

    Raises:
        LocalBrowserNotAllowedError: When the tier requires a remote
            browser but no remote CDP endpoint is configured.
    """
    if tier in _REMOTE_REQUIRED_TIERS and not connection.cdp_url:
        raise LocalBrowserNotAllowedError(
            tier=tier,
            details={"cdp_url": connection.cdp_url},
        )
