"""MattermostAdapter -- WebSocket platform adapter for arcgateway.

FedRAMP High / IL5 / JWICS chat surface for air-gapped DOE/National Lab
deployments.  Connects via MM's native WS API and posts replies via REST.

Protocol:
  WS  : wss://<host>/api/v4/websocket  (PAT in Authorization header)
  REST: POST /api/v4/posts              (Bearer token, JSON body)
  Event: {"event": "posted", "data": {"post": "<json-encoded-post>"}}

Design (mirroring SlackAdapter, per ADR-002):
  D-001  Per-channel bounded queue + drain task (web.py pattern).
  D-002  connect() starts _ws_loop as a background Task.
  D-003  Reconnect: 800ms -> x1.7 -> 15s cap.
  D-004  In-memory dedup on post_id.
  D-005  session_key = "mattermost:{channel_id}:{user_id}" (ADR-002).
  D-006  bot_token is private; never in repr/str/dir.
  D-007  Federal-tier guard: server_url must be RFC1918/loopback/intranet.
  D-008  aiohttp for WS + HTTP (lazy import; optional extra).

Audit events (all carry platform="mattermost"):
  gateway.adapter.register, gateway.message.delivered,
  gateway.message.dropped, gateway.adapter.disconnect
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway.adapters.mattermost")

_QUEUE_MAXSIZE = 100
_BACKOFF_INITIAL_MS: float = 800
_BACKOFF_FACTOR = 1.7
_BACKOFF_MAX_MS: float = 15_000
_MAX_MESSAGE_LENGTH = 4000

# SPEC-025 §L-2 — FIFO cap on the dedup ring. Once exceeded, oldest
# entries are evicted one-at-a-time so a reposted post_id within the
# window cannot replay (which a full ``set.clear()`` would allow).
_SEEN_POST_IDS_CAP = 10_000

_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def _split_message(text: str, max_length: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks at natural boundaries.

    Thin wrapper over the shared ``split_message`` helper (SPEC-025
    §arch-M-1). Mattermost prefers paragraph → newline → space because
    most posts are markdown-formatted; the space fallback keeps long
    single-paragraph posts from hard-cutting mid-word.
    """
    from arcgateway.adapters._text import split_message as _shared_split

    return _shared_split(
        text, max_length, boundaries=("\n\n", "\n", " ")
    )


def _is_intranet_host(hostname: str, intranet_domains: list[str]) -> bool:
    """Return True if hostname is RFC 1918 / loopback / in the allow-list.

    Used by the federal-tier air-gap guard (D-007). DNS resolution is
    synchronous — constructor-time validation, not a hot path.

    Matching contract:
      - ``intranet_domains`` entries match **exact hostname** only.
        ``mm.intranet.local`` is NOT covered by ``intranet.local``;
        the operator must list each FQDN that should be accepted.
      - ``localhost``, ``127.0.0.1``, ``::1`` always match.
      - Any other hostname must resolve to RFC 1918 / loopback for every
        returned addrinfo record.

    SPEC-025 §H-1 — DNS resolution failure (``gaierror``) is fail-CLOSED
    (returns ``False``). This forces the operator to add the hostname to
    ``intranet_domains`` explicitly when DNS is unreachable, instead of
    silently accepting hosts that may turn out to be public after DNS
    recovers.
    """
    if hostname in intranet_domains or hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not any(addr in net for net in _RFC1918_NETWORKS):
            return False
    return True


def _validate_intranet_domains(domains: list[str]) -> None:
    """Reject allow-list entries that resolve to public addresses.

    SPEC-025 §H-2 — without this, an operator misconfig of
    ``intranet_domains=["example.com"]`` silently bypasses the air-gap
    guard. Verify each entry is itself intranet at construction time so
    a bad allow-list fails loud, not silent. Loopback / RFC 1918 entries
    always pass; entries that fail DNS are accepted (matching the policy
    that ``intranet_domains`` is the explicit override for unresolvable
    hostnames — H-1 documents this contract).
    """
    for entry in domains:
        if entry in ("localhost", "127.0.0.1", "::1"):
            continue
        try:
            infos = socket.getaddrinfo(entry, None)
        except socket.gaierror:
            # Operator-supplied unresolvable name: this is the very
            # case ``intranet_domains`` exists for. Trust the operator.
            continue
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if not any(addr in net for net in _RFC1918_NETWORKS):
                msg = (
                    f"intranet_domains entry {entry!r} resolves to a public "
                    f"address ({addr}); refusing to start at federal tier."
                )
                raise ValueError(msg)


class MattermostAdapter:
    """Mattermost WebSocket adapter implementing BasePlatformAdapter.

    Authenticates via Personal Access Token (PAT).  At tier="federal",
    raises ValueError at construction if server_url resolves to a public
    host (air-gap enforcement per SPEC-025 §NFR-5).
    """

    name: str = "mattermost"

    def __init__(
        self,
        server_url: str,
        bot_token: str,
        on_message: Callable[[InboundEvent], Awaitable[None]],
        *,
        allowed_channel_ids: list[str] | None = None,
        bot_user_id: str = "",
        tier: str = "personal",
        intranet_domains: list[str] | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._bot_token = bot_token  # private -- never in repr
        self._on_message = on_message
        self._allowed_channel_ids: set[str] = (
            set(allowed_channel_ids) if allowed_channel_ids else set()
        )
        self._bot_user_id = bot_user_id
        self._tier = tier
        self._intranet_domains = intranet_domains or []

        # Federal-tier air-gap guard (D-007 + SPEC-025 §H-1/§H-2):
        #   1. Validate every entry in ``intranet_domains`` is itself intranet
        #      so a misconfigured allow-list cannot whitelist a public host.
        #   2. Resolve ``server_url`` hostname; raise ValueError if any
        #      returned address is public OR DNS fails (fail-closed per H-1).
        if tier == "federal":
            _validate_intranet_domains(self._intranet_domains)
            self._enforce_airgap_url(self._server_url)

        self._outbound_queues: dict[str, asyncio.Queue[str | None]] = {}
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}
        self._ws_task: asyncio.Task[None] | None = None
        self._connected = False
        # SPEC-025 §L-2 — FIFO eviction on overflow rather than full clear,
        # so a reposted post_id within the eviction window cannot replay.
        # The OrderedDict acts as a bounded LRU keyed on insertion order.
        self._seen_post_ids: OrderedDict[str, None] = OrderedDict()
        # SPEC-025 §L-1 — single aiohttp.ClientSession per adapter
        # lifetime. Created lazily on first connect/post (so personal-tier
        # tests that never network can still construct).
        self._http_session: Any | None = None

        self._audit("gateway.adapter.register", {
            "platform": "mattermost",
            "server_url": self._server_url,
            "tier": tier,
        })

    def _enforce_airgap_url(self, url: str) -> None:
        """Raise ValueError if server URL resolves to a public host (D-007)."""
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname or ""
        if not _is_intranet_host(hostname, self._intranet_domains):
            raise ValueError(
                f"MattermostAdapter: federal tier requires an intranet "
                f"server_url; {hostname!r} resolves to a public address. "
                "Configure server_url to an RFC 1918 / loopback address or add "
                "the hostname to intranet_domains."
            )

    # BasePlatformAdapter Protocol

    async def connect(self) -> None:
        """Start background WS loop.  Returns promptly."""
        try:
            import aiohttp  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "aiohttp is not installed. "
                "Install with: pip install 'arcgateway[mattermost]'"
            ) from exc
        _logger.info(
            "MattermostAdapter: connecting to %s (tier=%s)",
            self._server_url, self._tier,
        )
        self._ws_task = asyncio.create_task(
            self._ws_loop(), name="arcgateway.mattermost.ws_loop"
        )

    async def disconnect(self) -> None:
        """Stop all drain queues and cancel the WS loop."""
        self._connected = False
        for channel_id, queue in self._outbound_queues.items():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            task = self._drain_tasks.get(channel_id)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    _logger.debug(
                        "MattermostAdapter: drain task for %r stopped", channel_id
                    )
        self._drain_tasks.clear()
        self._outbound_queues.clear()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                _logger.debug("MattermostAdapter: ws_task stopped")
        # SPEC-025 §L-1 — close the shared aiohttp ClientSession so the
        # adapter doesn't leak open TCP connections across restarts.
        if self._http_session is not None and not self._http_session.closed:
            try:
                await self._http_session.close()
            except Exception:
                _logger.debug("MattermostAdapter: http session close failed")
        self._http_session = None
        self._ws_task = None
        self._audit("gateway.adapter.disconnect", {
            "platform": "mattermost", "server_url": self._server_url,
        })
        _logger.info("MattermostAdapter: disconnected")

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Enqueue message for async delivery.  Drop-oldest on full queue."""
        if not self._connected:
            self._audit_drop(target.chat_id, "not_connected")
            return
        for chunk in _split_message(message):
            queue = self._get_or_create_queue(target.chat_id)
            self._enqueue(target.chat_id, queue, chunk)

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
        await self.send(target, message)
        return None

    # Internal helpers

    def _audit_drop(self, channel_id: str, reason: str) -> None:
        """Emit a ``gateway.message.dropped`` audit event.

        SPEC-025 §arch-m-2 — five near-duplicate emit blocks consolidated
        here. Adding a new drop site is now one method call instead of a
        copy-paste foot-gun.
        """
        self._audit(
            "gateway.message.dropped",
            {
                "platform": "mattermost",
                "chat_id": channel_id,
                "reason": reason,
            },
        )

    def _get_or_create_queue(self, channel_id: str) -> asyncio.Queue[str | None]:
        if channel_id not in self._outbound_queues:
            queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            self._outbound_queues[channel_id] = queue
            self._drain_tasks[channel_id] = asyncio.create_task(
                self._drain_loop(channel_id, queue),
                name=f"arcgateway.mattermost.drain:{channel_id}",
            )
        return self._outbound_queues[channel_id]

    def _enqueue(
        self,
        channel_id: str,
        queue: asyncio.Queue[str | None],
        message: str,
    ) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                _ = queue.get_nowait()
                queue.put_nowait(message)
                self._audit_drop(channel_id, "queue_full_drop_oldest")
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                self._audit_drop(channel_id, "queue_unusable")

    async def _drain_loop(
        self,
        channel_id: str,
        queue: asyncio.Queue[str | None],
    ) -> None:
        try:
            while True:
                message = await queue.get()
                if message is None:
                    return  # sentinel
                await self._post_message(channel_id, message)
        except asyncio.CancelledError:
            return
        except Exception:
            _logger.exception(
                "MattermostAdapter: drain_loop error channel=%r", channel_id
            )

    async def _ensure_http_session(self) -> Any:
        """Lazily create the per-adapter aiohttp.ClientSession.

        SPEC-025 §L-1 — one ``ClientSession`` per adapter lifetime
        (recreated only when the previous one was closed by ``disconnect``)
        so connection-pooling + keep-alive work; previous code created a
        new session per outbound POST which broke pooling under load.
        """
        import aiohttp

        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _post_message(self, channel_id: str, message: str) -> None:
        """POST message to Mattermost REST API; emit audit on result.

        Logs only response-status metadata on failure — the response body
        could echo the bearer token in some misconfigurations and must
        never reach a log sink (SPEC-025 §L-3).
        """
        url = f"{self._server_url}/api/v4/posts"
        headers = {
            "Authorization": f"Bearer {self._bot_token}",
            "Content-Type": "application/json",
        }
        body = {"channel_id": channel_id, "message": message}
        try:
            session = await self._ensure_http_session()
            async with session.post(url, json=body, headers=headers) as resp:
                if resp.status in (200, 201):
                    self._audit("gateway.message.delivered", {
                        "platform": "mattermost",
                        "chat_id": channel_id,
                        "breakdown": {
                            "delivered": 1, "dropped_backpressure": 0, "dead": 0,
                        },
                    })
                else:
                    # NOTE: body deliberately NOT logged — see L-3 above.
                    _logger.warning(
                        "MattermostAdapter: POST /api/v4/posts failed "
                        "channel=%r status=%d",
                        channel_id, resp.status,
                    )
                    self._audit_drop(channel_id, f"http_{resp.status}")
        except Exception as exc:
            _logger.warning(
                "MattermostAdapter: _post_message error channel=%r: %s",
                channel_id, exc,
            )
            self._audit_drop(channel_id, "post_exception")

    async def _ws_loop(self) -> None:
        """Connect, read events, reconnect with 800ms -> x1.7 -> 15s backoff."""
        import aiohttp
        backoff_ms = _BACKOFF_INITIAL_MS
        ws_url = (
            self._server_url.replace("https://", "wss://").replace("http://", "ws://")
            + "/api/v4/websocket"
        )
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        ws_url, headers=headers, heartbeat=30
                    ) as ws:
                        self._connected = True
                        backoff_ms = _BACKOFF_INITIAL_MS
                        _logger.info("MattermostAdapter: WS connected to %s", ws_url)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_ws_message(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                _logger.warning(
                                    "MattermostAdapter: WS closed/error type=%s",
                                    msg.type,
                                )
                                break
            except asyncio.CancelledError:
                self._connected = False
                return
            except Exception as exc:
                _logger.warning(
                    "MattermostAdapter: WS error -- reconnecting in %.0fms: %s",
                    backoff_ms, exc,
                )
            self._connected = False
            await asyncio.sleep(backoff_ms / 1000.0)
            backoff_ms = min(backoff_ms * _BACKOFF_FACTOR, _BACKOFF_MAX_MS)

    async def _handle_ws_message(self, raw: str) -> None:
        """Parse MM 'posted' WS events and forward to on_message."""
        try:
            envelope: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            return
        if envelope.get("event") != "posted":
            return
        raw_post = envelope.get("data", {}).get("post")
        if not raw_post:
            return
        try:
            post: dict[str, Any] = (
                json.loads(raw_post) if isinstance(raw_post, str) else raw_post
            )
        except json.JSONDecodeError:
            return

        post_id: str = post.get("id", "")
        channel_id: str = post.get("channel_id", "")
        user_id: str = post.get("user_id", "")
        message: str = post.get("message", "") or ""

        if self._bot_user_id and user_id == self._bot_user_id:
            return
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return
        if post_id:
            if post_id in self._seen_post_ids:
                _logger.debug("MattermostAdapter: duplicate post_id=%r skipped", post_id)
                return
            self._seen_post_ids[post_id] = None
            # SPEC-025 §L-2 — FIFO eviction (drop oldest 10% rather than
            # full clear). Full-clear would open a brief window where a
            # reposted post_id is re-delivered; FIFO keeps the bound but
            # preserves the most recent ids as deduplication anchors.
            while len(self._seen_post_ids) > _SEEN_POST_IDS_CAP:
                self._seen_post_ids.popitem(last=False)
        if not message:
            return

        await self._on_message(InboundEvent(
            platform="mattermost",
            chat_id=channel_id,
            thread_id=None,
            user_did=f"mattermost:{user_id}",
            agent_did="",
            session_key=f"mattermost:{channel_id}:{user_id}",
            message=message,
            raw_payload=dict(post),
        ))

    def _audit(self, action: str, data: dict[str, Any]) -> None:
        """Emit audit event; swallowed per AU-5."""
        try:
            from arcgateway.audit import emit_event as _arc_emit
            _arc_emit(
                action=action,
                target=str(data.get("chat_id", data.get("server_url", "mattermost"))),
                outcome=data.get("outcome", "allow"),
                extra=data,
            )
        except Exception:
            _logger.exception("MattermostAdapter: audit emission failed")

    def __repr__(self) -> str:
        return (
            f"MattermostAdapter("
            f"server_url={self._server_url!r}, "
            f"tier={self._tier!r}, "
            f"connected={self._connected})"
        )

    def __str__(self) -> str:
        return repr(self)
