"""Composition root for the in-process gateway runtime.

When ArcUI hosts the runtime in-process (SPEC-023), it imports
``build_for_embedded`` from this module to wire up the same components
that ``arc gateway start`` builds in standalone mode: executor,
session router, and any enabled platform adapters. The result is a
small ``EmbeddedGateway`` named tuple stored on Starlette's
``app.state``.

Module boundary (SDD §2):
    bootstrap MAY import every other arcgateway module.
    Adapters MUST NOT import bootstrap (to keep them leaves of the
    dependency graph). The ``test_web_adapter_does_not_import_bootstrap``
    architecture test enforces this.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from arcgateway.executor import AsyncioExecutor, Executor
from arcgateway.session import SessionRouter
from arcgateway.stream_bridge import StreamBridge

if TYPE_CHECKING:
    from arcgateway.adapters.base import BasePlatformAdapter
    from arcgateway.adapters.web import WebPlatformAdapter
    from arcgateway.config import GatewayConfig

_logger = logging.getLogger("arcgateway.bootstrap")


class EmbeddedGateway(NamedTuple):
    """Bundle of components arcui needs to host the gateway runtime.

    ``web_adapter`` is the core in-process browser adapter (``None`` when the
    ``[platforms.web]`` block is disabled). ``adapters`` holds every enabled
    remote-platform adapter (telegram, slack, …) built through the generic
    adapter-plugin registry — the gateway core names none of them.
    """

    executor: Executor
    session_router: SessionRouter
    web_adapter: WebPlatformAdapter | None
    stream_bridge: StreamBridge
    adapters: tuple[BasePlatformAdapter, ...] = ()


def _load_did_index(team_root: Path) -> dict[str, Path]:
    """Build a single ``did → agent_dir`` map from all team TOMLs.

    One pass over ``team_root`` returns a dict the factory can consult
    on every chat turn without re-reading disk. Cache invalidation:
    callers are expected to re-index when the team_root mtime changes
    or on bootstrap restart.
    """
    if not team_root.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # pragma: no cover — Python <3.11 fallback
        import tomli as tomllib  # type: ignore[no-redef]  # reason: Python <3.11 fallback — tomli is the same API as stdlib tomllib

    index: dict[str, Path] = {}
    for agent_dir in sorted(team_root.glob("*_agent")):
        if not agent_dir.is_dir():
            continue
        toml_path = agent_dir / "arcagent.toml"
        if not toml_path.exists():
            continue
        try:
            cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        identity = cfg.get("identity", {}) if isinstance(cfg.get("identity"), dict) else {}
        did = identity.get("did")
        if isinstance(did, str) and did:
            index[did] = agent_dir
    return index


def _resolve_agent_dir(
    team_root: Path,
    agent_did: str,
    *,
    did_index: dict[str, Path] | None = None,
) -> Path | None:
    """Find the team/<name>_agent/ directory matching ``agent_did``.

    Uses the prebuilt ``did_index`` when supplied (the factory reuses
    one across turns); otherwise builds a one-shot index. Returns
    ``None`` when the DID is not in any TOML — well-formed configs
    must declare ``[identity].did`` in arcagent.toml.
    """
    if not team_root.exists():
        return None
    if did_index is None:
        did_index = _load_did_index(team_root)
    return did_index.get(agent_did)


def _make_agent_factory(team_root: Path) -> Any:
    """Build an async agent_factory bound to ``team_root``.

    The factory mirrors ``arccli.commands.agent._load_arcagent`` so the
    same agent definitions work in both daemon and embedded modes.
    Imports are lazy so arcgateway can be installed without arcagent
    in test environments.

    The DID-to-directory index is computed lazily on first call and
    refreshed on cache miss — so newly-added agents become reachable
    without restarting the gateway, and the steady-state path is a
    single dict lookup per chat turn.
    """
    cached_index: dict[str, dict[str, Path]] = {"map": {}}

    async def _factory(agent_did: str) -> Any:
        # Lazy import — arcagent is optional at install time for this package.
        from arcagent.core.agent import ArcAgent
        from arcagent.core.config import load_config

        agent_dir = _resolve_agent_dir(team_root, agent_did, did_index=cached_index["map"])
        if agent_dir is None:
            # Cache miss may mean the agent was added since startup;
            # rebuild the index once and retry.
            cached_index["map"] = _load_did_index(team_root)
            agent_dir = _resolve_agent_dir(team_root, agent_did, did_index=cached_index["map"])
        if agent_dir is None:
            msg = f"no agent under {team_root} matches {agent_did}"
            raise FileNotFoundError(msg)
        config_path = agent_dir / "arcagent.toml"
        if not config_path.exists():
            msg = f"arcagent.toml not found at {config_path}"
            raise FileNotFoundError(msg)

        config = load_config(config_path)
        arc_agent = ArcAgent(config, config_path=config_path)
        await arc_agent.startup()
        return arc_agent

    return _factory


def _build_executor(tier: str, agent_factory: Any) -> Executor:
    """Pick the executor class for the configured tier."""
    if tier == "federal":
        from arcgateway.executor_subprocess import SubprocessExecutor

        _logger.info("bootstrap: federal tier → SubprocessExecutor")
        return SubprocessExecutor(
            worker_cmd=[sys.executable, "-m", "arccli.agent_worker"],
        )
    _logger.info("bootstrap: %s tier → AsyncioExecutor", tier)
    return AsyncioExecutor(agent_factory)


def _build_web_adapter(
    cfg: GatewayConfig,
    session_router: SessionRouter,
) -> WebPlatformAdapter | None:
    """Build a WebPlatformAdapter when enabled, else return None."""
    if not cfg.platforms.web.enabled:
        return None
    from arcgateway.adapters.web import WebPlatformAdapter

    return WebPlatformAdapter(
        on_message=session_router.handle,
        agent_did=cfg.effective_agent_did("web"),
        max_connections=cfg.platforms.web.max_connections,
        idle_timeout_seconds=cfg.platforms.web.idle_timeout_seconds,
        max_frame_bytes=cfg.platforms.web.max_frame_bytes,
    )


async def build_for_embedded(
    team_root: Path,
    gateway_config: GatewayConfig,
) -> EmbeddedGateway:
    """Compose the in-process gateway runtime for arcui.

    Args:
        team_root: Directory containing one ``<name>_agent/`` subdirectory
            per agent. The agent_factory resolves DIDs against this root.
        gateway_config: Loaded GatewayConfig — selects tier, enables/
            disables adapters, and supplies per-adapter limits.

    Returns:
        EmbeddedGateway with executor, session_router, stream_bridge, and any
        enabled adapters. The arcui lifespan stores the named tuple on
        ``app.state`` and is responsible for ``await connect()`` /
        ``await disconnect()`` on each adapter.
    """
    if not team_root.exists():
        _logger.warning(
            "bootstrap: team_root %s does not exist — agent_factory will fail at runtime",
            team_root,
        )

    agent_factory = _make_agent_factory(team_root)
    executor = _build_executor(gateway_config.gateway.tier, agent_factory)
    session_router = SessionRouter(executor=executor)
    stream_bridge = StreamBridge()

    from arcgateway.adapters.registry import AdapterUnavailableError, build_adapters

    web_adapter = _build_web_adapter(gateway_config, session_router)

    # Remote platforms load through the generic adapter-plugin registry.
    # Federal tier fails closed (AdapterUnavailableError) so a misconfigured
    # deployment refuses to start rather than serve a silent subset.
    try:
        remote_adapters = build_adapters(
            platforms=gateway_config.platforms.remote_blocks(),
            on_message=session_router.handle,
            default_agent_did=gateway_config.gateway.agent_did,
            tier=gateway_config.gateway.tier,
        )
    except AdapterUnavailableError:
        _logger.exception("bootstrap: refusing to start — enabled adapter unavailable")
        raise

    # Wire the primary delivery adapter via the public setter.
    # SessionRouter needs an adapter for outbound delivery; each adapter
    # needs ``session_router.handle`` for inbound — two-step construction
    # breaks the cycle. Web is primary when present; else the first
    # available platform.
    primary = web_adapter or (remote_adapters[0] if remote_adapters else None)
    if primary is not None:
        session_router.set_adapter(primary)

    _logger.info(
        "bootstrap: embedded gateway built (tier=%s web=%s remote=%s)",
        gateway_config.gateway.tier,
        bool(web_adapter),
        [a.name for a in remote_adapters],
    )

    return EmbeddedGateway(
        executor=executor,
        session_router=session_router,
        web_adapter=web_adapter,
        stream_bridge=stream_bridge,
        adapters=tuple(remote_adapters),
    )


__all__ = [
    "EmbeddedGateway",
    "build_for_embedded",
]
