"""UI Reporter module — bridges agent events to ArcUI dashboard.

Subscribes to ModuleBus events, wraps them as UIEvent-compatible JSON,
and streams to an ArcUI server via WebSocket. Receives control messages
from the UI and re-emits them on the bus as ``ui:control`` events.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from arcui._constants import LOOPBACK_HOSTS
from pydantic import BaseModel, Field

from arcagent.core.module_bus import EventContext, ModuleContext

_logger = logging.getLogger("arcagent.ui_reporter")

# SR-1: token file MUST be 0600, owned by current UID. SR-6 / H-4: probe URL
# host MUST be loopback — autoconnect over a non-loopback URL is a config-
# poisoning exfiltration vector. Loopback set is shared with arccli via
# arcui._constants so the two callers can never disagree.

# Events from arcrun bridged as agent:pre_tool/post_tool etc.
# These map to UIEvent layer="run", not "agent".
_RUN_LAYER_SUFFIXES = frozenset(
    {
        "pre_tool",
        "post_tool",
        "pre_plan",
        "post_plan",
    }
)

# Known ModuleBus events to subscribe to.
_LLM_EVENTS = (
    "llm:call_complete",
    "llm:config_change",
    "llm:circuit_change",
)

_SCHEDULER_EVENTS = (
    "schedule:completed",
    "schedule:failed",
)

_AGENT_EVENTS = (
    "agent:init",
    "agent:ready",
    "agent:shutdown",
    "agent:pre_respond",
    "agent:post_respond",
    "agent:error",
    "agent:extensions_loaded",
    "agent:skills_loaded",
    "agent:tools_reloaded",
    "agent:pre_tool",
    "agent:post_tool",
    "agent:pre_plan",
    "agent:post_plan",
    "agent:pre_compaction",
)


class UIReporterConfig(BaseModel):
    """Configuration for the UI reporter module.

    `enabled = true` (the default) runs the auto-enable probe at startup:
    the module connects only if `~/.arcagent/ui-token` exists with safe
    perms AND the URL is reachable. `enabled = false` is an explicit
    opt-out — no probe, no connection.
    """

    enabled: bool = True
    url: str = "ws://localhost:8420/api/agent/connect"
    token: str = ""
    reconnect_max_interval: float = Field(default=60.0, gt=0)
    buffer_size: int = Field(default=1000, ge=1)


# Well-known shared token file — both `arc ui start` and agents read this
_TOKEN_FILE = Path.home() / ".arcagent" / "ui-token"


# Probe budget — fast enough that "no UI running" doesn't add measurable
# cold-start lag, generous enough that a real loopback HEAD on macOS (where
# the first httpx.Client.head() reproducibly takes ~95ms for socket+SSL
# context setup) doesn't false-negative. 50ms was the original budget, but
# benchmarks showed cold loopback HEAD at 90-150ms — every chat session
# was failing the probe and silently auto-disabling ui_reporter.
_PROBE_TIMEOUT_SECONDS = 0.5


def _open_token_file_secure(token_file: Path) -> tuple[str | None, str]:
    """Open the token file once with secure flags; return (token, reason).

    Delegates to `arcagent.utils.secure_file.read_secret_owned` (Wave 2
    TD-MED). The shared utility owns the SR-1 invariants (O_NOFOLLOW,
    fstat-on-same-fd, perm + owner check); this wrapper translates the
    `read_secret_owned` reason codes into `token_file_*` audit reasons
    so existing audit-trail consumers don't break.
    """
    from arcagent.utils.secure_file import read_secret_owned

    data, reason = read_secret_owned(token_file)
    if data is None:
        # Map shared reasons to SR-1 audit categories the existing
        # ui.agent_autoconnect consumers already know how to filter on.
        mapping = {
            "absent": "token_file_absent",
            "stat_failed": "token_file_stat_failed",
            "wrong_owner": "token_file_wrong_owner",
            "loose_perms": "token_file_loose_perms",
            "read_failed": "token_file_read_failed",
        }
        if reason.startswith("open_failed_"):
            return None, "token_file_" + reason
        return None, mapping.get(reason, f"token_file_{reason}")
    return (
        data.decode("utf-8", errors="replace").strip() or None,
        "ok",
    )


def _server_reachable(url: str) -> tuple[bool, str]:
    """HEAD probe to derived health URL; pin to loopback only.

    Refuses non-loopback URLs unconditionally (review finding H-4): a
    poisoned agent config could otherwise redirect autoconnect to an
    attacker host and leak the agent token via the WebSocket transport.
    """
    parsed = urlparse(url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        return False, "url_not_loopback"
    health_url = _derive_health_url(url)
    try:
        import httpx
    except ImportError:
        return False, "httpx_unavailable"
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = client.head(health_url)
    except httpx.HTTPError as exc:
        return False, f"probe_failed_{type(exc).__name__}"
    # 200 (returned by /api/health) and 405 (route exists, doesn't accept HEAD)
    # both prove the server is up; anything else is a miss.
    if response.status_code in (200, 405):
        return True, "probe_ok"
    return False, f"probe_status_{response.status_code}"


def _should_auto_enable(token_file: Path, url: str) -> tuple[bool, str, str | None]:
    """Compose the file-perm probe and the URL probe.

    Returns (enable, reason, token_bytes). On enable=True, token_bytes is
    the file contents read in the same fstat call that validated perms —
    callers MUST use it instead of re-reading the path (see review H-3).
    The `reason` string is logged and forwarded to the audit event so an
    auditor can tell "no UI running" from "operator forgot to chmod 0600".
    """
    token, file_reason = _open_token_file_secure(token_file)
    if token is None:
        return False, file_reason, None
    ok, server_reason = _server_reachable(url)
    if not ok:
        return False, server_reason, None
    return True, server_reason, token


def _derive_health_url(ws_url: str) -> str:
    """Map ws[s]://host:port/api/agent/connect → http[s]://host:port/api/health."""
    http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
    base, _, _ = http_url.partition("/api/")
    return base + "/api/health"


class UIReporterModule:
    """UI reporter — Module Bus participant.

    Wraps internal events as UIEvent-compatible payloads and streams
    them to the ArcUI server. Observational priority (200) ensures
    business logic runs first.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        workspace: Path = Path("."),
        transport: Any | None = None,
        **_kw: Any,
    ) -> None:
        self._config = UIReporterConfig(**(config or {}))
        self._workspace = workspace
        self._transport = transport
        self._sequence = 0
        self._agent_name = ""
        self._agent_id = ""
        self._source_id = ""

    @property
    def name(self) -> str:
        return "ui_reporter"

    async def startup(self, ctx: ModuleContext) -> None:
        """Subscribe to bus events for forwarding to UI.

        `enabled = false` returns early (explicit opt-out). Otherwise, the
        auto-enable probe runs: if the token file is present with safe perms
        AND the URL responds, the module emits `ui.agent_autoconnect` and
        connects. Probe failure is silent — the agent runs normally with no
        UI link.
        """
        if not self._config.enabled:
            _logger.debug("ui_reporter: explicitly disabled")
            return

        enable, reason, file_token = _should_auto_enable(_TOKEN_FILE, self._config.url)
        if not enable:
            _logger.debug("ui_reporter: auto-disabled (%s)", reason)
            return
        await self._emit_autoconnect_audit(ctx, reason)

        self._agent_name = ctx.config.agent.name
        self._agent_id = getattr(ctx.config.agent, "did", "") or self._agent_name
        self._source_id = getattr(ctx.config.agent, "did", "")

        # Token precedence: config > ARCUI_AGENT_TOKEN env > file bytes
        # already read by the secure probe. H-3: never re-read the path —
        # that re-opens the TOCTOU window between probe and use.
        token = self._config.token or os.environ.get("ARCUI_AGENT_TOKEN", "") or file_token or ""

        # Create transport if not injected and token is available
        if self._transport is None and token:
            try:
                from arcui.transport_ws import WebSocketTransport

                # Build registration payload for the UI server
                registration = {
                    "agent_name": self._agent_name,
                    "model": ctx.config.llm.model,
                    "provider": ctx.config.llm.model.split("/")[0]
                    if "/" in ctx.config.llm.model
                    else "unknown",
                    "workspace": str(self._workspace),
                    "modules": list(ctx.config.modules.keys()),
                }

                # Token provider re-reads the file on every reconnect so an
                # arcui restart (which rotates the agent_token) doesn't
                # permanently break this agent's UI connection. Falls back
                # to the static token captured at startup.
                config_token = self._config.token

                def _refresh_token() -> str:
                    if config_token:
                        return config_token
                    env_token = os.environ.get("ARCUI_AGENT_TOKEN", "")
                    if env_token:
                        return env_token
                    try:
                        return _TOKEN_FILE.read_text().strip()
                    except OSError:
                        return token

                self._transport = WebSocketTransport(
                    url=self._config.url,
                    token=token,
                    reconnect_cap=self._config.reconnect_max_interval,
                    buffer_size=self._config.buffer_size,
                    registration=registration,
                    token_provider=_refresh_token,
                )
                # Start the background connect loop
                self._transport.start()
            except ImportError:
                _logger.warning("arcui not installed, transport disabled")
        elif not token:
            _logger.warning(
                "No UI token found (config, ARCUI_AGENT_TOKEN env, or %s) "
                "— UI reporter will buffer but not connect",
                _TOKEN_FILE,
            )

        # Subscribe to LLM events
        for event in _LLM_EVENTS:
            ctx.bus.subscribe(
                event,
                self._on_event,
                priority=200,
                module_name="ui_reporter",
            )

        # Subscribe to agent/run events
        for event in _AGENT_EVENTS:
            ctx.bus.subscribe(
                event,
                self._on_event,
                priority=200,
                module_name="ui_reporter",
            )

        # Subscribe to scheduler events — schedule:completed and schedule:failed
        # are emitted by the scheduler module on every fire. Without these,
        # arcui has no signal for "the cron just ran" — breaking the schedule
        # history view for any agent that runs scheduled work.
        for event in _SCHEDULER_EVENTS:
            ctx.bus.subscribe(
                event,
                self._on_event,
                priority=200,
                module_name="ui_reporter",
            )

        _logger.info("UI reporter started, target=%s", self._config.url)

    async def shutdown(self) -> None:
        """Clean up resources."""
        if self._transport is not None:
            try:
                await self._transport.close()
            except Exception:
                _logger.debug("Error closing transport", exc_info=True)
        _logger.info("UI reporter shut down")

    async def _emit_autoconnect_audit(self, ctx: ModuleContext, reason: str) -> None:
        """Emit `ui.agent_autoconnect` (SPEC-019 SR-3, T5.2).

        Uses ctx.telemetry.audit_event when available so the event lands
        in the same tamper-evident chain as other agent audit records.
        The Pydantic model makes drop-a-field a validation error rather
        than a silent audit gap.

        Narrow exception list: telemetry may be missing the method
        (AttributeError), the model construction may fail validation
        (ValidationError → ValueError parent), or audit emission may
        IO-error on the audit sink (OSError). Anything broader hides
        real bugs.
        """
        from arcui.audit import AgentAutoconnectFields, UIAuditEvent
        from pydantic import ValidationError

        try:
            telemetry = ctx.telemetry
            audit_event = getattr(telemetry, "audit_event", None)
            if audit_event is None:
                return
            agent_id = getattr(ctx.config.agent, "did", "") or ctx.config.agent.name
            fields = AgentAutoconnectFields(
                agent_id=agent_id,
                uid=os.getuid(),
                url=self._config.url,
                reason=reason,
            )
            audit_event(UIAuditEvent.AGENT_AUTOCONNECT, fields.model_dump())
        except (AttributeError, OSError, ValidationError):  # pragma: no cover
            _logger.debug("ui.agent_autoconnect audit failed", exc_info=True)

    async def _on_event(self, ctx: EventContext) -> None:
        """Handle any subscribed bus event — wrap and forward to UI."""
        payload = self._wrap_event(ctx.event, ctx.data)
        _logger.debug("UI event: %s → layer=%s", ctx.event, payload["layer"])

        # Send via transport if available
        if self._transport is not None:
            try:
                from arcui.types import UIEvent

                event = UIEvent(**payload)
                await self._transport.send_event(self._agent_id, event)
            except Exception:
                _logger.debug("Failed to send event via transport", exc_info=True)

    def _wrap_event(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        """Convert a ModuleBus event into a UIEvent-compatible dict."""
        layer = self._classify_layer(event)
        event_type = event.split(":", 1)[1] if ":" in event else event

        seq = self._sequence
        self._sequence += 1

        return {
            "layer": layer,
            "event_type": event_type,
            "agent_id": self._agent_id,
            "agent_name": self._agent_name,
            "source_id": self._source_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": dict(data),
            "sequence": seq,
        }

    @staticmethod
    def _classify_layer(event: str) -> str:
        """Map a ModuleBus event name to a UIEvent layer."""
        if event.startswith("llm:"):
            return "llm"
        if event.startswith("schedule:"):
            return "scheduler"
        if event.startswith("agent:"):
            suffix = event.split(":", 1)[1]
            if suffix in _RUN_LAYER_SUFFIXES:
                return "run"
            return "agent"
        return "agent"


__all__ = ["UIReporterConfig", "UIReporterModule"]
