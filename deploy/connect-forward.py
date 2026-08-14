#!/usr/bin/env python3
"""Expose a host reachable only through a CONNECT proxy as a local TCP port.

Written for a box running tailscaled in userspace mode: no ordinary process can
open a socket to a ``100.x`` tailnet peer, only the daemon can. tailscaled's
outbound HTTP proxy speaks CONNECT, and this turns that into a plain port an
``arcllm`` ``base_url`` can point at.

**It refuses to look healthy when it is not.** The listener binds regardless of
whether the target exists, so the obvious implementation reports ``active`` to
systemd while every request through it dies — which is exactly what happened: the
target host left the tailnet, the unit stayed green for weeks, and every turn on
the agent behind it failed with a protocol error that named nothing. So:

* the target is **required**, never defaulted — a stale built-in default is the
  original bug, and a wrong host must fail at configuration time, not silently;
* the target is probed **before** binding, so a broken bridge never accepts a
  connection it cannot serve;
* the target is re-probed **periodically**, and the process exits when it goes
  away, so a unit with ``Restart=always`` visibly cycles instead of sitting green.

Standard library only: it runs under the system interpreter, outside any venv.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Coroutine
from typing import Any

_logger = logging.getLogger("arc.connect-forward")

#: How often to re-check that the target is still reachable, in seconds.
HEALTH_INTERVAL = float(os.environ.get("ARC_FORWARD_HEALTH_INTERVAL", "30"))

#: How long a CONNECT handshake may take before the target counts as unreachable.
CONNECT_TIMEOUT = float(os.environ.get("ARC_FORWARD_CONNECT_TIMEOUT", "10"))

_BUFFER = 65536


class TargetUnreachableError(RuntimeError):
    """The CONNECT proxy could not reach the target host."""


def _split_hostport(value: str, *, what: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"{what} must be 'host:port', got {value!r}")
    return host, int(port)


async def open_tunnel(
    proxy: tuple[str, int], target: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """CONNECT through ``proxy`` to ``target``; return the tunnelled streams.

    Raises:
        TargetUnreachableError: the proxy is down, the handshake timed out, or
            the proxy answered anything other than 2xx.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(*proxy), timeout=CONNECT_TIMEOUT
        )
    except (OSError, TimeoutError) as exc:
        raise TargetUnreachableError(
            f"CONNECT proxy {proxy[0]}:{proxy[1]} is not accepting: {exc}"
        ) from exc

    try:
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT)
        # Drain the response headers up to the blank line before handing over.
        while (await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT)) not in (
            b"\r\n",
            b"",
        ):
            pass
    except (OSError, TimeoutError) as exc:
        writer.close()
        raise TargetUnreachableError(f"CONNECT to {target} failed: {exc}") from exc

    if b" 2" not in status[:12]:
        writer.close()
        detail = status.decode(errors="replace").strip()
        raise TargetUnreachableError(f"CONNECT to {target} refused by the proxy: {detail!r}")
    return reader, writer


async def probe(proxy: tuple[str, int], target: str) -> None:
    """Open and immediately close a tunnel — the health check.

    Raises:
        TargetUnreachableError: propagated from :func:`open_tunnel`.
    """
    _reader, writer = await open_tunnel(proxy, target)
    writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(_BUFFER):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, TimeoutError):
        pass
    finally:
        writer.close()


def make_handler(
    proxy: tuple[str, int], target: str
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, None]]:
    """Build the per-connection handler that splices a client to the target."""

    async def handle(
        client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            up_reader, up_writer = await open_tunnel(proxy, target)
        except TargetUnreachableError as exc:
            _logger.error("refusing a client: %s", exc)
            client_writer.close()
            return
        await asyncio.gather(
            _pipe(client_reader, up_writer),
            _pipe(up_reader, client_writer),
        )

    return handle


async def watch(proxy: tuple[str, int], target: str, server: asyncio.AbstractServer) -> None:
    """Re-probe forever; stop the server when the target goes away.

    A bridge whose target has vanished must not keep answering. Exiting hands
    the decision to systemd, which restarts and re-probes — so the unit's state
    tracks reality instead of tracking whether a socket is bound.
    """
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        try:
            await probe(proxy, target)
        except TargetUnreachableError as exc:
            _logger.error("target became unreachable, exiting so the unit reflects it: %s", exc)
            server.close()
            return


async def serve(listen: tuple[str, int], proxy: tuple[str, int], target: str) -> None:
    """Probe the target, then serve until it becomes unreachable."""
    await probe(proxy, target)
    _logger.info("target %s reachable via %s:%d", target, *proxy)

    server = await asyncio.start_server(make_handler(proxy, target), *listen)
    _logger.info("forwarding %s:%d -> %s", listen[0], listen[1], target)
    async with server:
        watcher = asyncio.create_task(watch(proxy, target, server))
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            watcher.cancel()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    target = os.environ.get("ARC_FORWARD_TARGET", "")
    if not target:
        _logger.error(
            "ARC_FORWARD_TARGET is required (e.g. 'inference-1:4000'). It is never "
            "defaulted: a built-in host that outlives the machine it named is how "
            "this bridge came to report healthy while forwarding nothing."
        )
        return 2

    listen = _split_hostport(
        os.environ.get("ARC_FORWARD_LISTEN", "127.0.0.1:4000"), what="ARC_FORWARD_LISTEN"
    )
    proxy = _split_hostport(
        os.environ.get("ARC_FORWARD_PROXY", "127.0.0.1:1055"), what="ARC_FORWARD_PROXY"
    )

    try:
        asyncio.run(serve(listen, proxy, target))
    except TargetUnreachableError as exc:
        _logger.error("refusing to start: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
