"""Journey J1-2: OAuth from any browser, every step in ArcUI, no terminal.

The customer's browser is on another computer. They open Settings → Access, save
the dashboard's public address, then click Connect on a Google connection. The
provider sends the browser to ``<public address>/oauth/callback``, the callback
page finishes the sign-in, and the connection turns healthy.

Real routes (Settings, connector, OAuth), real custody and the real config file.
Only the provider's HTTP is fake.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from arcui.routes.ui_settings import routes as ui_settings_routes

from packages.arcui.tests.test_oauth_routes import EMAIL, OAuthWorld, world

__all__ = ["world"]

PUBLIC = "https://arc.tail1234.ts.net"
#: The remote browser reaches the dashboard through a proxy that rewrites Host.
REMOTE_BROWSER = {"Host": "10.0.0.7:8420", "X-Forwarded-Host": "evil.example"}


def test_set_public_address_in_settings_then_connect_from_a_remote_browser(world: Path) -> None:
    arc = OAuthWorld(world)
    arc.client.app.router.routes.extend(ui_settings_routes)
    arc.client.app.state.ui_tls_active = False
    operator = {**arc.headers(), **REMOTE_BROWSER}

    # 1. Settings → Access: save the address the customer's browser uses.
    saved = arc.client.put(
        "/api/settings/public-address", json={"public_base_url": PUBLIC + "/"}, headers=operator
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["redirect_uri"] == f"{PUBLIC}/oauth/callback"

    # 2. Connections → Google: the app-slot form shows the address to register.
    app = arc.client.get("/api/oauth-apps/google", headers=operator).json()
    assert app["redirect_uri"] == f"{PUBLIC}/oauth/callback"
    arc.install()
    arc.set_app()

    # 3. Connect: the provider is told to come back to the public address.
    begun = arc.client.post("/api/connections/blackarc/oauth/begin", json={}, headers=operator)
    assert begun.status_code == 200, begun.text
    query = parse_qs(urlsplit(begun.json()["authorize_url"]).query)
    assert query["redirect_uri"] == [f"{PUBLIC}/oauth/callback"]

    # 4. Consent: the browser lands on <public>/oauth/callback, which finishes it.
    landed = arc.provider.consent(begun.json()["authorize_url"], email=EMAIL)
    assert landed.startswith(f"{PUBLIC}/oauth/callback?")
    done = arc.client.post("/api/oauth/complete", json={"redirect_url": landed}, headers=operator)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "healthy", done.json()
    assert arc.custody("refresh_token") is not None
