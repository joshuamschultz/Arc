"""Download tool — file download via CDP Browser.setDownloadBehavior.

Downloads are gated by ``security.allow_downloads`` in config.
Files are downloaded to the configured download path.
URL must pass security policy before download is initiated.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.tools.navigate import _check_url_policy

_logger = logging.getLogger("arcagent.modules.browser.tools.download")


def create_download_tools(
    cdp: Any,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create download tools.

    Returns:
        List containing browser_download_file tool.
    """

    async def _handle_download(url: str) -> str:
        """Download a file by navigating to its URL.

        Validates URL against security policy first, then sets
        download behavior and navigates to trigger download.
        """
        # Enforce URL security policy before downloading
        _check_url_policy(url, config.security)

        download_path = config.security.download_path

        # Configure Chrome to allow downloads to the specified path
        await cdp.send(
            "Browser",
            "setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": download_path,
            },
        )

        # Navigate to the URL to trigger download
        await cdp.send("Page", "navigate", {"url": url})

        await bus.emit(
            "browser.download_started",
            {"url": url, "path": download_path},
        )
        _logger.info("Download started: %s → %s", url, download_path)

        return f"Download started: {url} → {download_path}"

    return [
        RegisteredTool(
            name="browser_download_file",
            description=(
                "Download a file by navigating to its URL. "
                "Files are saved to the configured download path. "
                "URL must pass security policy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the file to download",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_download,
            timeout_seconds=config.timeouts.navigate,
        ),
    ]
