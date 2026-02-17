"""Browser tools factory — creates and returns all browser tools.

Each tool file exports a create_*_tools() function. This module's
create_browser_tools() orchestrates them all based on config toggles.
"""

from __future__ import annotations

from typing import Any

from arcagent.core.tool_registry import RegisteredTool
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.tools.cookies import create_cookie_tools
from arcagent.modules.browser.tools.dialog import create_dialog_tools
from arcagent.modules.browser.tools.download import create_download_tools
from arcagent.modules.browser.tools.form import create_form_tools
from arcagent.modules.browser.tools.interact import create_interact_tools
from arcagent.modules.browser.tools.javascript import create_javascript_tools
from arcagent.modules.browser.tools.navigate import create_navigate_tools
from arcagent.modules.browser.tools.read import create_read_tools
from arcagent.modules.browser.tools.screenshot import create_screenshot_tools


def create_browser_tools(
    cdp: Any,
    ax: Any,
    config: BrowserConfig,
    bus: Any,
) -> list[RegisteredTool]:
    """Create all browser tools based on config.

    Args:
        cdp: CDPClientManager instance.
        ax: AccessibilityManager instance.
        config: Browser module config.
        bus: ModuleBus instance.

    Returns:
        List of RegisteredTool instances ready for ToolRegistry.
    """
    tools: list[RegisteredTool] = []

    tools.extend(create_navigate_tools(cdp, config, bus))
    tools.extend(create_read_tools(cdp, ax, config, bus))
    tools.extend(create_screenshot_tools(cdp, config, bus))
    tools.extend(create_interact_tools(cdp, ax, config, bus))
    tools.extend(create_form_tools(cdp, ax, config, bus))
    tools.extend(create_dialog_tools(cdp, config, bus))
    tools.extend(create_cookie_tools(cdp, config, bus))

    if config.security.allow_js_execution:
        tools.extend(create_javascript_tools(cdp, config, bus))
    if config.security.allow_downloads:
        tools.extend(create_download_tools(cdp, config, bus))

    return tools
