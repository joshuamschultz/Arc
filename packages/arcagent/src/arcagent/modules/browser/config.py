"""Configuration for the browser module.

Owned by the browser module — not part of core config.
Loaded from ``[modules.browser.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from arcagent.core.module_config import ModuleConfig


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
    # A directly-connectable page target ("page") vs a browser-level
    # endpoint ("browser") that requires discovering and attaching to a
    # page target first. Local launch and most raw remote endpoints are
    # "page"; managed services (Browserbase, Steel, Browserless) hand out
    # a "browser" endpoint.
    endpoint_kind: Literal["page", "browser"] = "page"


class BrowserbaseConfig(ModuleConfig):
    """Managed Browserbase backend settings (``provider = "browserbase"``).

    The API key is resolved through the tier-aware secret resolver under
    the name ``browserbase_api_key`` (federal: vault-mandatory; personal
    and enterprise fall back to ``api_key_env``). Never store the key
    here.
    """

    project_id: str = ""
    api_key_env: str = "BROWSERBASE_API_KEY"
    api_base: str = "https://api.browserbase.com/v1"
    region: str = ""
    proxies: bool = False
    keep_alive: bool = False
    request_timeout_s: float = 30.0


class BrowserUseConfig(ModuleConfig):
    """Agentic ``browser_task`` tool settings (optional ``browser-use`` extra).

    Off by default. When enabled, ``browser_task`` runs a bounded
    browser-use agent whose LLM is routed through arcllm (so PII
    redaction, audit, and provider config all apply). Forbidden at the
    federal tier regardless of this flag.
    """

    enabled: bool = False
    llm_provider: str = "anthropic"
    llm_model: str = ""  # empty → the provider's configured default
    max_steps: int = 25
    max_actions_per_step: int = 5
    use_vision: bool = True
    step_timeout_s: int = 180
    # Attach the agent to an existing browser over CDP. Empty → browser-use
    # manages its own browser. For a managed service, pass its connect URL.
    cdp_url: str = ""


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

    # Which backend produces the browser session. "cdp" (default) is the
    # zero-dependency, federal-safe Chrome DevTools Protocol backend
    # (local launch or remote attach). "browserbase" uses the managed
    # Browserbase service. Attach any other CDP-speaking service by
    # adding a backend file — see modules/browser/backends/.
    provider: Literal["cdp", "browserbase"] = "cdp"

    # Deployment tier — drives the federal remote-browser requirement
    # (see policy.enforce_sandbox_policy). Federal forbids launching a
    # local headless Chrome; it must attach to a remote CDP endpoint.
    tier: str = "personal"
    security: BrowserSecurityConfig = Field(default_factory=BrowserSecurityConfig)
    connection: BrowserConnectionConfig = Field(default_factory=BrowserConnectionConfig)
    browserbase: BrowserbaseConfig = Field(default_factory=BrowserbaseConfig)
    browser_use: BrowserUseConfig = Field(default_factory=BrowserUseConfig)
    cookies: BrowserCookieConfig = Field(default_factory=BrowserCookieConfig)
    accessibility_tree_depth: int = 10
    chrome_memory_limit_mb: int = 512
