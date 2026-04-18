"""Redis-backed leader election — SPEC-017 R-048.

Fallback for deployments without Kubernetes Leases available. Uses
the SET NX PX primitive with a Lua-based fence token check on release.

Usage:

    election = RedisLockElection(
        redis=redis_async_client,
        key="arcagent:proactive:leader",
        identity=socket.gethostname(),
    )
    await election.acquire_or_wait()
    try:
        await engine.start_tick_loop()
    finally:
        await election.release()

Failure modes:

- Redis connection failure → fail-closed (``acquire_or_wait`` returns
  ``False`` after logging).
- Lock TTL expires during work → background renewer extends TTL; if
  it fails, ``is_leader()`` drops to ``False`` and the engine exits
  its tick loop.
- Split-brain via partition → the lock holder on one side loses their
  TTL; the other side takes over. Both believing themselves leader
  simultaneously is bounded by the renewal cycle; actions are
  idempotent (per SPEC-017 R-048) so the bounded overlap is safe.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

_logger = logging.getLogger("arcagent.proactive.leader_redis")

_DEFAULT_TTL_MS = 30_000
_DEFAULT_RENEW_INTERVAL_S = 10.0

# Lua script for safe release — only delete if we still hold the lock.
# Prevents deleting a lock that another replica legitimately acquired
# after our TTL expired.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua script for safe renewal — extend TTL only if we still hold.
_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisLockElection:
    """LeaderElection satisfied by a Redis lock (SET NX PX + fence token).

    Parameters
    ----------
    redis:
        An async redis-py client with ``set``, ``eval``, ``close`` methods.
    key:
        Redis key the lock lives at. Shared across all replicas.
    identity:
        Unique per-replica identifier; used as the fence token.
    ttl_ms:
        Lock TTL in milliseconds. Renewed periodically.
    renew_interval_s:
        Renewal cadence. Must be < ``ttl_ms / 1000``.
    """

    def __init__(
        self,
        *,
        redis: Any,
        key: str,
        identity: str,
        ttl_ms: int = _DEFAULT_TTL_MS,
        renew_interval_s: float = _DEFAULT_RENEW_INTERVAL_S,
    ) -> None:
        if renew_interval_s * 1000 >= ttl_ms:
            msg = (
                f"renew_interval_s ({renew_interval_s}) × 1000 must be "
                f"strictly less than ttl_ms ({ttl_ms})"
            )
            raise ValueError(msg)
        self._redis = redis
        self._key = key
        # Fence token: identity + random suffix so stale renewals from
        # a prior process instance with the same hostname can't refresh
        # a new process's lock.
        self._fence_token = f"{identity}:{secrets.token_hex(8)}"
        self._ttl_ms = ttl_ms
        self._renew_interval_s = renew_interval_s
        self._acquired = False
        self._renewer: asyncio.Task[None] | None = None

    async def acquire_or_wait(self, timeout: float = 30.0) -> bool:
        """Try to acquire the lock; return ``False`` on any failure."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                won = await self._redis.set(
                    self._key,
                    self._fence_token,
                    nx=True,
                    px=self._ttl_ms,
                )
            except Exception:
                _logger.exception("Redis SET failed — treating as not leader")
                return False
            if won:
                self._acquired = True
                self._renewer = asyncio.create_task(self._renew_loop())
                return True
            await asyncio.sleep(1.0)
        return False

    async def release(self) -> None:
        """Release the lock via fence-token-checked DEL."""
        self._acquired = False
        if self._renewer is not None:
            self._renewer.cancel()
            try:
                await self._renewer
            except asyncio.CancelledError:
                pass
            self._renewer = None
        try:
            await self._redis.eval(
                _RELEASE_SCRIPT, 1, self._key, self._fence_token
            )
        except Exception:
            _logger.debug("Lock release failed (may have expired)", exc_info=True)

    def is_leader(self) -> bool:
        return self._acquired

    # --- Internals --------------------------------------------------------

    async def _renew_loop(self) -> None:
        """Periodically PEXPIRE the lock so it doesn't expire mid-run."""
        while self._acquired:
            try:
                await asyncio.sleep(self._renew_interval_s)
                extended = await self._redis.eval(
                    _RENEW_SCRIPT, 1, self._key, self._fence_token, self._ttl_ms
                )
                if not extended:
                    # Someone else holds the lock now; we lost it.
                    _logger.warning(
                        "Redis leader lock %r lost during renewal", self._key
                    )
                    self._acquired = False
                    return
            except asyncio.CancelledError:
                return
            except Exception:
                _logger.exception("Redis lock renewal failed")
                self._acquired = False
                return


__all__ = ["RedisLockElection"]
