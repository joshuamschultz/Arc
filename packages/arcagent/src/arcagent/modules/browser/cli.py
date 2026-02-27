"""CLI commands for the browser module — connection and debug tools.

Provides ``arc agent browser <subcommand>`` for testing and debugging
browser module connectivity without a full agent session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import click


def cli_group(workspace: Path) -> click.Group:
    """Factory: return a Click group bound to *workspace*."""

    @click.group("browser")
    @click.pass_context
    def browser(ctx: click.Context) -> None:
        """Browser module — CDP connection and debugging tools."""
        ctx.ensure_object(dict)
        ctx.obj["workspace"] = workspace

    @browser.command("status")
    @click.pass_context
    def status(ctx: click.Context) -> None:
        """Show browser module connection status."""
        from arccli.formatting import print_kv

        from arcagent.modules.browser.config import BrowserConfig

        ws: Path = ctx.obj["workspace"]
        config = BrowserConfig()

        print_kv(
            [
                ("Module", "browser"),
                ("Workspace", str(ws)),
                ("Headless", str(config.connection.headless)),
                ("CDP URL", config.connection.cdp_url or "(auto-launch)"),
                ("URL mode", config.security.url_mode),
                ("JS execution", str(config.security.allow_js_execution)),
                ("Downloads", str(config.security.allow_downloads)),
            ]
        )

    @browser.command("navigate")
    @click.argument("url")
    @click.pass_context
    def navigate(ctx: click.Context, url: str) -> None:
        """Quick-test: navigate to a URL and print the page title."""
        from arccli.formatting import click_echo

        from arcagent.modules.browser.cdp_client import CDPClientManager
        from arcagent.modules.browser.config import BrowserConfig
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()

        # Enforce URL security policy before navigating
        _check_url_policy(url, config.security)

        async def _run() -> None:
            cdp = CDPClientManager(config.connection)
            try:
                await cdp.connect()
                click_echo(f"Connected: {cdp.url}")

                await cdp.send("Page", "navigate", {"url": url})
                title_result = await cdp.send(
                    "Runtime", "evaluate", {"expression": "document.title"}
                )
                title = title_result.get("result", {}).get("value", "(no title)")
                click_echo(f"Title: {title}")
            finally:
                await cdp.disconnect()

        asyncio.run(_run())

    @browser.command("screenshot")
    @click.argument("url")
    @click.option("--output", "-o", default="screenshot.png", help="Output file path.")
    @click.pass_context
    def screenshot(ctx: click.Context, url: str, output: str) -> None:
        """Quick-test: navigate to URL and save a screenshot."""
        import base64

        from arccli.formatting import click_echo

        from arcagent.modules.browser.cdp_client import CDPClientManager
        from arcagent.modules.browser.config import BrowserConfig
        from arcagent.modules.browser.tools.navigate import _check_url_policy

        config = BrowserConfig()

        # Enforce URL security policy before navigating
        _check_url_policy(url, config.security)

        async def _run() -> None:
            cdp = CDPClientManager(config.connection)
            try:
                await cdp.connect()
                await cdp.send("Page", "navigate", {"url": url})

                result = await cdp.send(
                    "Page",
                    "captureScreenshot",
                    {"format": "png"},
                )
                data = base64.b64decode(result.get("data", ""))
                Path(output).write_bytes(data)
                click_echo(f"Screenshot saved: {output} ({len(data)} bytes)")
            finally:
                await cdp.disconnect()

        asyncio.run(_run())

    return browser
