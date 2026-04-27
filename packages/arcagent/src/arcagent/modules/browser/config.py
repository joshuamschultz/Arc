"""Configuration for the browser module.

Owned by the browser module — not part of core config.
Loaded from ``[modules.browser.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from arcagent.modules.base_config import ModuleConfig


class BrowserSecurityConfig(ModuleConfig):
    """Security controls for the browser module.

    Configurable URL access policy, scheme blocking, and capability
    toggles. Inherits ``extra="forbid"`` from ModuleConfig.
    """

    url_mode: Literal["allowlist", "denylist"] = "denylist"
    url_patterns: list[str] = Field(default_factory=list)
    blocked_schemes: list[str] = Field(
        default_factory=lambda: [
            "file",
            "chrome",
            "chrome-extension",
            "javascript",
            "data",
            "blob",
            "ftp",
        ]
    )
    allow_js_execution: bool = True
    allow_downloads: bool = True
    download_path: str = "/tmp/arcagent-downloads"  # noqa: S108 — default; overridden in production config
    redact_inputs: bool = False
    max_page_text_length: int = 50_000
    max_screenshot_width: int = 1920
    max_screenshot_height: int = 1080


class BrowserConnectionConfig(ModuleConfig):
    """CDP connection settings.

    When ``cdp_url`` is empty, the module auto-launches a headless
    Chrome process with ``--remote-debugging-port``.
    """

    cdp_url: str = ""
    chrome_path: str = ""
    headless: bool = True
    remote_debugging_port: int = 0
    chrome_flags: list[str] = Field(default_factory=list)
    startup_timeout_seconds: int = 10


class BrowserTimeoutConfig(ModuleConfig):
    """Per-tool timeout defaults (seconds).

    These values are set on ``RegisteredTool.timeout_seconds`` at
    registration time. ArcRun enforces them via ``asyncio.wait_for``.
    """

    navigate: int = 30
    click: int = 5
    # ``type`` is a Python builtin — use ``type_`` with alias
    type_: int = Field(default=5, alias="type")
    screenshot: int = 10
    read_page: int = 15
    execute_js: int = 10
    fill_form: int = 30
    default: int = 10


class BrowserCookieConfig(ModuleConfig):
    """Cookie persistence settings.

    Ephemeral by default. When ``persist`` is True, cookies are
    encrypted at rest using a Fernet key from the environment.
    """

    persist: bool = False
    encryption_key_env: str = "ARCAGENT_BROWSER_COOKIE_KEY"
    storage_path: str = ""


class BrowserConfig(ModuleConfig):
    """Root browser module config.

    Composes all sub-configs with sensible defaults. Works
    out-of-the-box with zero configuration.
    """

    security: BrowserSecurityConfig = Field(default_factory=BrowserSecurityConfig)
    connection: BrowserConnectionConfig = Field(default_factory=BrowserConnectionConfig)
    timeouts: BrowserTimeoutConfig = Field(default_factory=BrowserTimeoutConfig)
    cookies: BrowserCookieConfig = Field(default_factory=BrowserCookieConfig)
    accessibility_tree_depth: int = 10
    chrome_memory_limit_mb: int = 512


class PlaywrightConfig(ModuleConfig):
    """Playwright browser provider configuration.

    Controls sandbox isolation mode and whether the browser runs locally
    (headless in the same process) or via a remote provider (e.g., Browserbase).

    Used by ``enforce_sandbox_policy()`` to validate tier-specific rules before
    any browser session is created.
    """

    mode: Literal["local", "remote"] = "local"
    sandbox: Literal["loose", "strict"] = "loose"
    remote_provider: str = ""
    remote_endpoint: str = ""
