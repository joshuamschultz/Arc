"""ConfigController — runtime get/set for LLM configuration.

Immutable ConfigSnapshot with atomic swap on patch. Audit trail via
TraceRecord emission on every change.
"""

import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from arcllm.exceptions import ArcLLMConfigError
from arcllm.trace_store import TraceRecord


class ConfigSnapshot(BaseModel, frozen=True):
    """Immutable configuration snapshot. Frozen Pydantic model."""

    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    daily_budget_limit: float | None = None
    monthly_budget_limit: float | None = None
    failover_chain: list[str] = []


_PATCHABLE_KEYS = {
    "model",
    "temperature",
    "max_tokens",
    "daily_budget_limit",
    "monthly_budget_limit",
    "failover_chain",
}


class ConfigController:
    """Runtime get/set for LLM configuration with atomic swap and audit trail.

    Thread-safe. All mutations go through patch() which validates, creates a new
    frozen snapshot, and fires on_change callbacks and on_event TraceRecords.
    """

    def __init__(
        self,
        initial: dict[str, Any],
        *,
        on_event: Callable[[TraceRecord], None] | None = None,
    ) -> None:
        self._snapshot = ConfigSnapshot(**initial)
        self._lock = threading.Lock()
        self._on_change_callbacks: list[Callable[[ConfigSnapshot], None]] = []
        self._on_event = on_event

    def get_snapshot(self) -> ConfigSnapshot:
        """Return current immutable configuration snapshot."""
        with self._lock:
            return self._snapshot

    def patch(
        self, updates: dict[str, Any], *, actor: str
    ) -> ConfigSnapshot:
        """Apply updates atomically. Returns new snapshot.

        Args:
            updates: Dict of field→new_value. Only patchable keys accepted.
            actor: Identity of who made the change (for audit trail).

        Returns:
            New ConfigSnapshot after applying updates.

        Raises:
            ArcLLMConfigError: On invalid keys or values.
        """
        invalid_keys = set(updates.keys()) - _PATCHABLE_KEYS
        if invalid_keys:
            raise ArcLLMConfigError(
                f"Cannot patch keys: {sorted(invalid_keys)}. "
                f"Patchable: {sorted(_PATCHABLE_KEYS)}"
            )
        if not updates:
            raise ArcLLMConfigError("patch() requires at least one update")

        with self._lock:
            old = self._snapshot
            changes: dict[str, dict[str, Any]] = {}

            # Build change diff
            old_dict = old.model_dump()
            for key, new_val in updates.items():
                old_val = old_dict.get(key)
                if old_val != new_val:
                    changes[key] = {"old": old_val, "new": new_val}

            if not changes:
                return old  # No actual changes

            # Validate by constructing new snapshot (Pydantic will validate)
            try:
                new = old.model_copy(update=updates)
            except Exception as e:
                raise ArcLLMConfigError(f"Invalid config update: {e}") from e

            # Atomic swap
            self._snapshot = new

        # Fire callbacks outside lock
        for cb in self._on_change_callbacks:
            cb(new)

        # Emit audit TraceRecord
        if self._on_event is not None:
            record = TraceRecord(
                provider="system",
                model="system",
                event_type="config_change",
                event_data={
                    "actor": actor,
                    "changes": changes,
                },
            )
            self._on_event(record)

        return new

    def on_change(self, callback: Callable[[ConfigSnapshot], None]) -> None:
        """Register a callback fired after every successful patch()."""
        self._on_change_callbacks.append(callback)
