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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from arcgateway.attachment_scanner import AttachmentScanner, CleanScanner
from arcgateway.broker_bootstrap import BrokerHandle, start_broker
from arcgateway.commands import build_default_registry
from arcgateway.executor import AsyncioExecutor, Executor
from arcgateway.media_store import MediaStore
from arcgateway.parts import MediaPart, Part
from arcgateway.session import SessionRouter
from arcgateway.stream_bridge import StreamBridge

if TYPE_CHECKING:
    from arcgateway.adapters.base import BasePlatformAdapter
    from arcgateway.adapters.web import WebPlatformAdapter
    from arcgateway.config import GatewayConfig
    from arcgateway.workflow_runner_host import RunnerHost

_logger = logging.getLogger("arcgateway.bootstrap")


# Inbound artefact ceiling (SPEC-065 REQ-299). 20 MiB is the smallest ceiling
# the supported platforms impose on a bot download (Telegram's Bot API caps
# getFile at 20 MB), so a larger value here would accept artefacts the adapter
# could never fetch. Not yet operator-configurable — the spec leaves retention
# and sizing open, and one honest constant beats a config key nothing reads.
_MEDIA_CEILING_BYTES = 20 * 1024 * 1024

AttachmentScannerFactory = Callable[[Path], AttachmentScanner]


class EmbeddedGateway(NamedTuple):
    """Bundle of components arcui needs to host the gateway runtime.

    ``web_adapter`` is the core in-process browser adapter (``None`` when the
    ``[platforms.web]`` block is disabled). ``adapters`` holds every enabled
    remote-platform adapter (telegram, slack, …) built through the generic
    adapter-plugin registry — the gateway core names none of them.

    ``broker`` (COMP-008) is the message broker this startup ensured, and the
    right to stop it if this startup is what started it. Its ``available`` flag
    is what lets a messaging surface tell "nothing to show" apart from "cannot
    see" (REQ-307); the host is responsible for ``await broker.aclose()`` on
    shutdown, alongside the adapter disconnects.

    ``workflow_runner_host`` (SPEC-061 COMP-009) is the singleton ArcFlow
    runner host constructed on THIS (the agent) side of the fleet service —
    ``None`` when arcteam's workflow engine has not landed in this checkout
    yet, or a runner is already active elsewhere in this process. Its
    presence here — not in arcui — is what makes execution independent of
    the dashboard (REQ-230).
    """

    executor: Executor
    session_router: SessionRouter
    web_adapter: WebPlatformAdapter | None
    stream_bridge: StreamBridge
    broker: BrokerHandle
    adapters: tuple[BasePlatformAdapter, ...] = ()
    workflow_runner_host: RunnerHost | None = None


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
    # Discover by the presence of arcagent.toml — the same signal team_roster
    # uses — so both `arc agent create <name>` (bare `<name>/`) and the legacy
    # `<name>_agent/` layout resolve. Globbing `*_agent` missed bare dirs, so
    # chat runs failed with "no agent matches DID" while the roster still listed
    # them (the executor could never find the directory).
    for toml_path in sorted(team_root.glob("*/arcagent.toml")):
        agent_dir = toml_path.parent
        if not agent_dir.is_dir():
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


def _make_agent_factory(
    team_root: Path,
    deliver_for: Callable[[str], Any] | None = None,
) -> Any:
    """Build an async agent_factory bound to ``team_root``.

    The factory mirrors ``arccli.commands.agent._load_arcagent`` so the
    same agent definitions work in both daemon and embedded modes.
    Imports are lazy so arcgateway can be installed without arcagent
    in test environments.

    ``deliver_for(agent_did)`` returns that agent's channel deliver fn (or None).
    It is late-bound because the SessionRouter it closes over is built after this
    factory (two-step wiring in build_for_embedded); the factory calls it per
    agent-construction and injects the result before startup() so a newly-started
    agent's scheduler delivers through THIS agent's bot.

    The DID-to-directory index is computed lazily on first call and
    refreshed on cache miss — so newly-added agents become reachable
    without restarting the gateway, and the steady-state path is a
    single dict lookup per chat turn.
    """
    cached_index: dict[str, dict[str, Path]] = {"map": {}}

    async def _factory(agent_did: str) -> Any:
        # MSG4: reuse the always-on fleet's already-started instance when one is
        # running, so web chat and the messaging inbox loop share ONE ArcAgent
        # (one durable NATS consumer) per agent instead of racing two.
        from arcgateway.fleet import current_fleet

        fleet = current_fleet()
        if fleet is not None:
            existing = fleet.get(agent_did)
            if existing is not None:
                return existing

        # Lazy import — arcagent is optional at install time for this package.
        import arcagent

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

        config = arcagent.load_config(config_path)
        arc_agent = arcagent.ArcAgent(config, config_path=config_path)
        # Inject channel delivery BEFORE startup so agent:ready carries it and
        # the scheduler can bind it (fleet-started agents get it in ui.py).
        if deliver_for is not None:
            deliver_fn = deliver_for(agent_did)
            if deliver_fn is not None:
                arc_agent.set_channel_deliver_fn(deliver_fn)
        await arc_agent.startup()
        return arc_agent

    return _factory


def _build_executor(tier: str, agent_factory: Any, team_root: Path) -> Executor:
    """Pick the executor class for the configured tier.

    Federal's SubprocessExecutor receives this gateway's own ``team_root`` so
    the spawned arc-agent-worker resolves ``--did`` against a real DID index
    (task 26) instead of a fixed, agent-agnostic search path that could load
    any agent's config regardless of which agent_did a session was for.
    """
    if tier == "federal":
        from arcgateway.executor_subprocess import SubprocessExecutor

        _logger.info("bootstrap: federal tier → SubprocessExecutor")
        return SubprocessExecutor(
            worker_cmd=[sys.executable, "-m", "arccli.agent_worker"],
            team_root=team_root,
        )
    _logger.info("bootstrap: %s tier → AsyncioExecutor", tier)
    return AsyncioExecutor(agent_factory)


def _build_web_adapter(
    cfg: GatewayConfig,
    session_router: SessionRouter,
    media_store_for: Callable[[str], MediaStore | None],
) -> WebPlatformAdapter | None:
    """Build a WebPlatformAdapter when enabled, else return None."""
    if not cfg.platforms.web.enabled:
        return None
    from arcgateway.adapters.web import WebPlatformAdapter

    def claim(
        user_did: str,
        agent_did: str,
        session_key: str,
        _chat_id: str,
        ids: list[str],
    ) -> list[Part]:
        store = media_store_for(agent_did)
        if store is None or len(ids) != len(set(ids)):
            raise ValueError("attachments unavailable")
        return [
            MediaPart(
                kind=cast(Literal["image", "file", "audio"], stored.kind),
                mime=stored.mime,
                declared_name=stored.declared_name,
                ref=stored.ref,
            )
            for stored in (
                store.claim(
                    attachment_id=attachment_id,
                    owner_did=user_did,
                    agent_did=agent_did,
                    session_key=session_key,
                )
                for attachment_id in ids
            )
        ]

    return WebPlatformAdapter(
        on_message=session_router.handle,
        claim_attachments=claim,
        agent_did=cfg.effective_agent_did("web"),
        max_connections=cfg.platforms.web.max_connections,
        idle_timeout_seconds=cfg.platforms.web.idle_timeout_seconds,
        max_frame_bytes=cfg.platforms.web.max_frame_bytes,
    )


async def build_for_embedded(
    team_root: Path,
    gateway_config: GatewayConfig,
    *,
    inbox_service: Any | None = None,
    attachment_scanner_factory: AttachmentScannerFactory | AttachmentScanner | None = None,
) -> EmbeddedGateway:
    """Compose the in-process gateway runtime for arcui.

    Args:
        team_root: Directory containing one ``<name>_agent/`` subdirectory
            per agent. The agent_factory resolves DIDs against this root.
        gateway_config: Loaded GatewayConfig — selects tier, enables/
            disables adapters, and supplies per-adapter limits.

    Returns:
        EmbeddedGateway with executor, session_router, stream_bridge, the
        ensured ``broker``, and any enabled adapters. The arcui lifespan stores
        the named tuple on ``app.state`` and is responsible for
        ``await connect()`` / ``await disconnect()`` on each adapter and for
        ``await broker.aclose()`` on shutdown.
    """
    if not team_root.exists():
        _logger.warning(
            "bootstrap: team_root %s does not exist — agent_factory will fail at runtime",
            team_root,
        )

    # COMP-008 / REQ-306: the broker is part of starting Arc, not of one CLI
    # verb. Every launch path composes through this function, so ensuring it
    # here — before the agents, adapters and runner that talk over it — is what
    # makes a plain start produce a working inbox. Never fatal (REQ-307): an
    # unreachable broker leaves an explicit unavailable handle on the bundle and
    # the rest of the gateway still serves.
    broker = await start_broker()
    try:
        return await _compose_embedded(
            team_root,
            gateway_config,
            broker,
            inbox_service=inbox_service,
            attachment_scanner_factory=attachment_scanner_factory,
        )
    except BaseException:
        # A broker started moments ago and abandoned here would outlive the
        # process that started it. Reuse is not ownership, so aclose() is a
        # no-op when the broker was already running.
        await broker.aclose()
        raise


async def _compose_embedded(
    team_root: Path,
    gateway_config: GatewayConfig,
    broker: BrokerHandle,
    *,
    inbox_service: Any | None = None,
    attachment_scanner_factory: AttachmentScannerFactory | AttachmentScanner | None = None,
) -> EmbeddedGateway:
    """Wire the components onto an already-ensured broker (see build_for_embedded)."""
    # Late-bound holder: the factory needs a per-agent deliver fn that closes
    # over the SessionRouter built below (two-step wiring breaks the
    # inbound/outbound cycle). The factory calls deliver_for(agent_did) per
    # construction; it resolves through the holder once the router exists.
    router_holder: dict[str, Any] = {"router": None}

    def _deliver_for(agent_did: str) -> Any:
        router = router_holder["router"]
        if router is None:
            return None
        from arcgateway.channel_delivery import make_channel_deliver_fn

        return make_channel_deliver_fn(router, agent_did)

    agent_factory = _make_agent_factory(team_root, _deliver_for)
    executor = _build_executor(gateway_config.gateway.tier, agent_factory, team_root)

    # [security].require_pairing activates DM pairing enforcement. This is
    # the PRODUCTION path — arcui hosts the runtime via build_for_embedded,
    # not GatewayRunner — so wiring pairing only into GatewayRunner.from_config
    # would leave every real deployment's pairing permanently disabled
    # regardless of config. Mirrors GatewayRunner.from_config's wiring.
    #
    # user_allowlist (task #34) is seeded ONLY inside this block — seeding it
    # while require_pairing=false would make PairingInterceptor start denying
    # non-allowlisted users from OTHER platforms (e.g. web) that reach
    # SessionRouter with no adapter-level allowlist gate of their own, a
    # regression for deployments this branch is not meant to touch.
    pairing_store: Any | None = None
    user_allowlist: set[str] | None = None
    if gateway_config.security.require_pairing:
        from arcgateway.pairing import PairingStore
        from arcgateway.pairing_allowlist import build_user_allowlist

        pairing_store = PairingStore(
            db_path=gateway_config.pairing.db_path,
            tier=gateway_config.gateway.tier,
        )
        user_allowlist = build_user_allowlist(gateway_config.platforms)
        _logger.info(
            "bootstrap: require_pairing=true — PairingStore wired (db=%s)",
            gateway_config.pairing.db_path,
        )

    # Slash-command registry + persisted session-rotation epochs. The epoch DB
    # sits beside the pairing DB so "New session" survives a gateway restart.
    command_registry = build_default_registry()
    # Deployment workflows become ``/name`` commands. specs() reads the on-disk
    # bundle store, so ordering vs the runner host below does not matter — a
    # workflow is in the menu whether or not the runner is up; run() resolves
    # the live runner lazily via RunnerHost.active() at call time.
    from arcgateway.commands.workflow_provider import GatewayWorkflowProvider

    command_registry.set_workflow_provider(GatewayWorkflowProvider())

    attachments_enabled = gateway_config.platforms.web.enabled or bool(
        gateway_config.platforms.remote_blocks()
    )

    def _scanner_for(agent_dir: Path) -> AttachmentScanner:
        if attachment_scanner_factory is None:
            scanner: AttachmentScanner = CleanScanner()
        elif isinstance(attachment_scanner_factory, AttachmentScanner):
            scanner = attachment_scanner_factory
        else:
            scanner = attachment_scanner_factory(agent_dir)
        if gateway_config.gateway.tier == "federal" and isinstance(scanner, CleanScanner):
            raise RuntimeError(
                "federal gateway requires an injected attachment scanner; CleanScanner is "
                "only valid for personal/test deployments"
            )
        return scanner

    if gateway_config.gateway.tier == "federal" and attachments_enabled:
        if attachment_scanner_factory is None:
            raise RuntimeError(
                "federal gateway requires an injected attachment scanner when messaging "
                "attachments are enabled"
            )
        if isinstance(attachment_scanner_factory, CleanScanner):
            raise RuntimeError(
                "federal gateway rejects CleanScanner; inject a real attachment scanner"
            )
        for agent_dir in _load_did_index(team_root).values():
            _scanner_for(agent_dir)

    def _media_store_for(agent_did: str) -> MediaStore | None:
        """Resolve the addressed agent's own artefact store (SPEC-065 COMP-002).

        Per agent, never one for the router: the router serves the whole fleet
        but a workspace belongs to exactly one agent (ADR-029). A single shared
        store would file every agent's inbound attachments in one agent's home,
        putting one correspondent's files inside another agent's readable
        workspace. An unknown DID yields None, and the message still arrives
        with its artefacts named rather than stored.
        """
        agent_dir = _resolve_agent_dir(team_root, agent_did)
        if agent_dir is None:
            return None
        return MediaStore(
            workspace=agent_dir / "workspace",
            max_bytes=_MEDIA_CEILING_BYTES,
            scanner=_scanner_for(agent_dir),
        )

    session_router = SessionRouter(
        executor=executor,
        pairing_store=pairing_store,
        user_allowlist=user_allowlist,
        command_registry=command_registry,
        session_epoch_db_path=gateway_config.pairing.db_path.parent / "session_epochs.db",
        media_store_for=_media_store_for,
        inbox_service=inbox_service,
    )
    # Now that the router exists, satisfy the factory's late-bound delivery hook.
    router_holder["router"] = session_router
    stream_bridge = StreamBridge()

    from arcgateway.adapters.registry import AdapterUnavailableError, build_adapters

    web_adapter = _build_web_adapter(gateway_config, session_router, _media_store_for)

    # Remote platforms load through the generic adapter-plugin registry.
    # Federal tier fails closed (AdapterUnavailableError) so a misconfigured
    # deployment refuses to start rather than serve a silent subset.
    try:
        remote_adapters = build_adapters(
            platforms=gateway_config.platforms.remote_blocks(),
            on_message=session_router.handle,
            default_agent_did=gateway_config.gateway.agent_did,
            tier=gateway_config.gateway.tier,
            require_pairing=gateway_config.security.require_pairing,
        )
    except AdapterUnavailableError:
        _logger.exception("bootstrap: refusing to start — enabled adapter unavailable")
        raise

    # Register EVERY adapter with the session router so each platform's
    # replies stream back to that platform (per-platform routing). Two-step
    # construction breaks the inbound/outbound cycle: build the router first,
    # build adapters with a closure over ``router.handle``, then register each.
    for outbound in (web_adapter, *remote_adapters):
        if outbound is not None:
            session_router.register_adapter(outbound)

    # Hand the command set (name + description) to any adapter that publishes a
    # native menu — Slack subscribes each command; Telegram calls setMyCommands.
    # command_specs() carries built-ins plus every workflow, so the menu is a
    # boot-time snapshot of both.
    for outbound in remote_adapters:
        set_names = getattr(outbound, "set_command_names", None)
        if callable(set_names):
            set_names(command_registry.command_specs())

    _logger.info(
        "bootstrap: embedded gateway built (tier=%s web=%s remote=%s broker=%s)",
        gateway_config.gateway.tier,
        bool(web_adapter),
        [a.name for a in remote_adapters],
        "available" if broker.available else "UNAVAILABLE",
    )

    # SPEC-061 COMP-009: the ArcFlow runner is constructed HERE, on the agent
    # side of the fleet service — never in arcui's lifespan (REQ-230). This is
    # the only integration point; a headless (arcui-absent) invocation of
    # this same function progresses workflows identically. Fail-open: a
    # checkout without arcteam's workflow engine yet still boots the rest of
    # the embedded gateway (`workflow_runner_host` is simply ``None``).
    from arcgateway.workflow_runner_host import start_runner_host

    workflow_runner_host = await start_runner_host(tier=gateway_config.gateway.tier)

    return EmbeddedGateway(
        executor=executor,
        session_router=session_router,
        web_adapter=web_adapter,
        stream_bridge=stream_bridge,
        broker=broker,
        adapters=tuple(remote_adapters),
        workflow_runner_host=workflow_runner_host,
    )


__all__ = [
    "EmbeddedGateway",
    "build_for_embedded",
]
