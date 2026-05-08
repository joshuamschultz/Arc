"""Postgres-backed PairingStore stub (T1.8.4 — deferred).

Sibling of ``arcgateway.pairing``. The SQLite-backed ``PairingStore`` is the
canonical implementation today; this module reserves the Postgres surface
for federal multi-instance deployments. Kept isolated so the SQLite path
isn't tangled with future Postgres-specific imports (asyncpg / psycopg3).
"""

from __future__ import annotations


class PostgresPairingStore:
    """Postgres-backed PairingStore for federal multi-instance deployments.

    Uses SELECT FOR UPDATE (pessimistic lock) on approval so that two gateway
    instances cannot double-consume the same code under concurrent load.

    TODO(T1.8.4): Implement. Required for federal deployments with >1 gateway
    instance behind a load balancer. See PLAN.md T1.8.4 and SDD §3.1 DM Pairing
    (Federal multi-instance Postgres backend). Requires asyncpg or psycopg3.
    Connection string from Vault (federal tier) per D-14.
    """

    def __init__(self, dsn: str, *, federal_tier: bool = True) -> None:
        raise NotImplementedError(
            "PostgresPairingStore is not yet implemented. "
            "Use PairingStore (SQLite) for single-instance deployments. "
            "See PLAN.md T1.8.4 for the multi-instance Postgres backend design."
        )
