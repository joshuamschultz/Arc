"""Kubernetes Lease-backed leader election — SPEC-017 R-048.

Production-grade implementation of :class:`LeaderElection` using the
``coordination.k8s.io/v1`` Lease resource. Only instantiated inside a
Kubernetes cluster (the `kubernetes` Python client is imported
lazily; absence raises a clear error).

Usage:

    election = KubernetesLeaseElection(
        namespace="arc-prod",
        lease_name="arcagent-proactive",
        identity=socket.gethostname(),
    )
    await election.acquire_or_wait()
    try:
        await engine.start_tick_loop()
    finally:
        await election.release()

Failure modes:

- Client construction failure → fail-closed (``acquire_or_wait``
  returns ``False`` after logging the exception).
- Lease contention → the caller does NOT become leader; the engine
  does not tick. Another instance holds the lease for its TTL.
- Lease expiration mid-run → the engine detects a lost lease via
  ``is_leader()`` returning ``False`` and exits. Operator pages on the
  schedule-gap metric.

This module is kept SEPARATE from ``leader.py`` so the core path
doesn't depend on the kubernetes client package.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

_logger = logging.getLogger("arcagent.proactive.leader_k8s")

# Default TTL and renewal interval. Lease TTL must be strictly greater
# than renewal interval so a transient hiccup doesn't lose the lease.
_DEFAULT_LEASE_SECONDS = 30
_DEFAULT_RENEW_SECONDS = 10


class KubernetesLeaseElection:
    """LeaderElection satisfied by a Kubernetes Lease resource.

    Parameters
    ----------
    namespace:
        K8s namespace the Lease lives in.
    lease_name:
        Lease object name — shared across all contending replicas.
    identity:
        Unique per-replica identifier (typically ``socket.gethostname()``).
    lease_seconds:
        Lease TTL. Must exceed ``renew_seconds`` by at least 3×.
    renew_seconds:
        How often the holder renews. Runs in a background task.
    """

    def __init__(
        self,
        *,
        namespace: str,
        lease_name: str,
        identity: str,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        renew_seconds: int = _DEFAULT_RENEW_SECONDS,
    ) -> None:
        if lease_seconds <= renew_seconds:
            msg = (
                f"lease_seconds ({lease_seconds}) must exceed "
                f"renew_seconds ({renew_seconds})"
            )
            raise ValueError(msg)
        self._namespace = namespace
        self._lease_name = lease_name
        self._identity = identity
        self._lease_seconds = lease_seconds
        self._renew_seconds = renew_seconds
        self._acquired = False
        self._renewer: asyncio.Task[None] | None = None
        self._coord_v1: Any = None

    async def acquire_or_wait(self, timeout: float = 30.0) -> bool:
        """Try to acquire the Lease; return ``False`` on any failure.

        Fail-closed: K8s client errors, permission denials, or
        contention all result in ``False`` so a misconfigured replica
        never mistakenly believes it is leader.
        """
        try:
            self._coord_v1 = await asyncio.to_thread(self._build_client)
        except Exception:
            _logger.exception("Failed to construct kubernetes coordination client")
            return False

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if await self._try_acquire_once():
                self._acquired = True
                self._renewer = asyncio.create_task(self._renew_loop())
                return True
            await asyncio.sleep(1.0)
        return False

    async def release(self) -> None:
        """Release the Lease + cancel the renewer."""
        self._acquired = False
        if self._renewer is not None:
            self._renewer.cancel()
            try:
                await self._renewer
            except asyncio.CancelledError:
                pass
            self._renewer = None
        try:
            await asyncio.to_thread(self._delete_lease)
        except Exception:
            _logger.debug("Lease delete failed (may already be gone)", exc_info=True)

    def is_leader(self) -> bool:
        return self._acquired

    # --- Internals --------------------------------------------------------

    def _build_client(self) -> Any:
        """Late-import the kubernetes client; raises if unavailable."""
        try:
            from kubernetes import client, config  # type: ignore[import-untyped]
        except ImportError as err:
            msg = (
                "KubernetesLeaseElection requires the 'kubernetes' package. "
                "Install with 'pip install kubernetes'."
            )
            raise RuntimeError(msg) from err

        try:
            config.load_incluster_config()
        except Exception:
            # Fallback for operator laptops: load ~/.kube/config
            config.load_kube_config()
        return client.CoordinationV1Api()

    async def _try_acquire_once(self) -> bool:
        """Attempt a single create-or-takeover cycle. Returns True on win."""
        try:
            return await asyncio.to_thread(self._sync_try_acquire)
        except Exception:
            _logger.debug("Lease acquire attempt failed", exc_info=True)
            return False

    def _sync_try_acquire(self) -> bool:
        """Blocking acquire logic — runs in a thread."""
        try:
            from kubernetes import client  # type: ignore[import-untyped]
            from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]
        except ImportError:
            return False

        now = datetime.now(UTC)
        lease_body = client.V1Lease(
            metadata=client.V1ObjectMeta(name=self._lease_name),
            spec=client.V1LeaseSpec(
                holder_identity=self._identity,
                lease_duration_seconds=self._lease_seconds,
                acquire_time=now,
                renew_time=now,
            ),
        )

        try:
            existing = self._coord_v1.read_namespaced_lease(
                name=self._lease_name, namespace=self._namespace
            )
        except ApiException as err:
            if err.status == 404:
                self._coord_v1.create_namespaced_lease(
                    namespace=self._namespace, body=lease_body
                )
                return True
            return False

        if self._holder_is_stale(existing, now):
            # Takeover: the previous holder hasn't renewed.
            existing.spec.holder_identity = self._identity
            existing.spec.acquire_time = now
            existing.spec.renew_time = now
            self._coord_v1.replace_namespaced_lease(
                name=self._lease_name,
                namespace=self._namespace,
                body=existing,
            )
            return True

        return existing.spec.holder_identity == self._identity

    @staticmethod
    def _holder_is_stale(lease: Any, now: datetime) -> bool:
        """Return True if the current holder's lease has expired."""
        renew_time = lease.spec.renew_time
        lease_duration = lease.spec.lease_duration_seconds or _DEFAULT_LEASE_SECONDS
        if renew_time is None:
            return True
        return now > renew_time + timedelta(seconds=lease_duration)

    async def _renew_loop(self) -> None:
        """Periodically refresh ``renew_time`` until cancelled."""
        while self._acquired:
            try:
                await asyncio.sleep(self._renew_seconds)
                await asyncio.to_thread(self._sync_renew)
            except asyncio.CancelledError:
                return
            except Exception:
                _logger.exception("Lease renewal failed")
                self._acquired = False
                return

    def _sync_renew(self) -> None:
        """Blocking renewal — writes ``renew_time = now``."""
        try:
            from kubernetes import client  # type: ignore[import-untyped]
        except ImportError:
            return
        existing = self._coord_v1.read_namespaced_lease(
            name=self._lease_name, namespace=self._namespace
        )
        if existing.spec.holder_identity != self._identity:
            # Lost the lease — another replica took over.
            self._acquired = False
            return
        existing.spec.renew_time = datetime.now(UTC)
        self._coord_v1.replace_namespaced_lease(
            name=self._lease_name,
            namespace=self._namespace,
            body=existing,
        )
        _ = client  # used for symmetry with _sync_try_acquire

    def _delete_lease(self) -> None:
        if self._coord_v1 is None:
            return
        try:
            self._coord_v1.delete_namespaced_lease(
                name=self._lease_name, namespace=self._namespace
            )
        except Exception:
            _logger.debug("Lease delete raised", exc_info=True)


__all__ = ["KubernetesLeaseElection"]
