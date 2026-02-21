"""Context Manager — system prompt assembly, token counting, pruning.

Monitors token budget and applies graduated strategies:
- Prune threshold (70%): Observation masking (replace old tool outputs)
- Compact threshold (85%): LLM summarization (stub in Phase 3)
- Emergency threshold (95%): Force truncation
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

# TYPE_CHECKING-only import to avoid circular dependency
from typing import TYPE_CHECKING, Any

from arcagent.core.config import ContextConfig
from arcagent.core.telemetry import AgentTelemetry

if TYPE_CHECKING:
    from arcagent.core.module_bus import ModuleBus

_logger = logging.getLogger("arcagent.context_manager")

# Core workspace files that compose the system prompt
_CORE_PROMPT_FILES = ["identity.md", "context.md"]

# Approximate characters per token for estimation
_CHARS_PER_TOKEN = 4


def _msg_attr(msg: Any, key: str, default: Any = "") -> Any:
    """Extract attribute from a message (dict or Pydantic model)."""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _msg_content_str(msg: Any) -> str:
    """Extract string content from a message, or empty string."""
    content = _msg_attr(msg, "content", "")
    return content if isinstance(content, str) else ""


class ContextManager:
    """Manages context window: prompt assembly, token tracking, pruning."""

    def __init__(
        self,
        config: ContextConfig,
        telemetry: AgentTelemetry | Any,
        bus: ModuleBus | None = None,
    ) -> None:
        self._config = config
        self._telemetry = telemetry
        self._bus = bus
        self._reported_input_tokens: int = 0
        self._reported_output_tokens: int = 0

    @property
    def reported_input_tokens(self) -> int:
        return self._reported_input_tokens

    @property
    def reported_output_tokens(self) -> int:
        return self._reported_output_tokens

    async def assemble_system_prompt(self, workspace: Path) -> str:
        """Build system prompt from workspace files.

        Reads core files (identity.md, context.md), then emits
        agent:assemble_prompt so modules can inject their own sections.
        Final ordering: identity first, context last, rest alphabetically.
        """
        sections: dict[str, str] = {}
        for filename in _CORE_PROMPT_FILES:
            filepath = workspace / filename
            if filepath.exists():
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    sections[filename.removesuffix(".md")] = content

        # Let modules inject content via event
        if self._bus is not None:
            await self._bus.emit(
                "agent:assemble_prompt",
                {"sections": sections, "workspace": str(workspace)},
            )

        # Dynamic ordering: identity first, context last, rest sorted
        parts: list[str] = []
        if sections.get("identity"):
            parts.append(f"--- identity ---\n{sections['identity']}")

        middle_keys = sorted(k for k in sections if k not in ("identity", "context"))
        for key in middle_keys:
            if sections[key]:
                parts.append(f"--- {key} ---\n{sections[key]}")

        if sections.get("context"):
            parts.append(f"--- context ---\n{sections['context']}")

        return "\n\n".join(parts)

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count with conservative multiplier.

        Uses character-based heuristic (~4 chars/token) with
        configurable multiplier (default 1.1x).
        """
        if not text:
            return 0
        raw = len(text) / _CHARS_PER_TOKEN
        return math.ceil(raw * self._config.estimate_multiplier)

    def usage_ratio(self, text: str) -> float:
        """Calculate usage ratio (estimated tokens / max tokens)."""
        return self.estimate_tokens(text) / self._config.max_tokens

    def token_ratio(self) -> float:
        """Calculate ratio from accumulated provider-reported usage."""
        if self._config.max_tokens == 0:
            return 0.0
        return self._reported_input_tokens / self._config.max_tokens

    def _estimate_ratio(self, messages: list[Any]) -> float:
        """Estimate token usage ratio for a message list."""
        total_text = "".join(_msg_content_str(m) for m in messages)
        return self.estimate_tokens(total_text) / self._config.max_tokens

    def update_reported_usage(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Accumulate provider-reported token usage."""
        self._reported_input_tokens += input_tokens
        self._reported_output_tokens += output_tokens

    def prune_observations(
        self,
        messages: list[Any],
        protected_recent_tokens: int = 40000,
    ) -> list[Any]:
        """Replace old tool outputs with placeholders.

        Observation masking (JetBrains Research): replaces old tool
        outputs with '[output pruned — N tokens]' while preserving
        tool call metadata and recent outputs.
        """
        if not messages:
            return messages

        # Calculate token budget for recent messages (from end)
        # Everything within the protected_recent_tokens window is safe
        recent_tokens = 0
        protected_start = 0  # Default: protect all messages
        for i in range(len(messages) - 1, -1, -1):
            content = _msg_content_str(messages[i])
            if content:
                msg_tokens = self.estimate_tokens(content)
                recent_tokens += msg_tokens
                if recent_tokens > protected_recent_tokens:
                    protected_start = i + 1
                    break

        # Prune old tool outputs (before protected zone)
        result = []
        for i, msg in enumerate(messages):
            role = _msg_attr(msg, "role", "")
            content = _msg_content_str(msg)

            if i >= protected_start or role != "tool" or not content:
                result.append(msg)
                continue

            placeholder = f"[output pruned — {self.estimate_tokens(content)} tokens]"
            if isinstance(msg, dict):
                pruned_msg = {**msg, "content": placeholder}
            else:
                pruned_msg = msg.model_copy(update={"content": placeholder})
            result.append(pruned_msg)

        return result

    def transform_context(self, messages: list[Any]) -> list[Any]:
        """Callback for arcrun.run(transform_context=...).

        Called before each LLM call. Applies graduated token management:
        1. Estimate current usage
        2. If > prune_threshold: mask old tool outputs
        3. If > compact_threshold: trigger memory flush (async event)
        4. If > emergency_threshold: force truncation
        """
        if not messages:
            return messages

        ratio = self._estimate_ratio(messages)

        # Below prune threshold — no action
        if ratio < self._config.prune_threshold:
            return messages

        # Above prune threshold — mask old tool outputs
        protected = int(self._config.max_tokens * 0.4)
        result = self.prune_observations(messages, protected_recent_tokens=protected)

        # Re-estimate after pruning
        ratio = self._estimate_ratio(result)

        # Above compact threshold — trigger pre-compaction memory flush
        if ratio >= self._config.compact_threshold and self._bus is not None:
            # Emit event for memory module (async, non-blocking)
            # Memory module will handle creating daily notes and saving context
            import asyncio
            try:
                task = asyncio.create_task(
                    self._bus.emit(
                        "agent:pre_compaction",
                        {"messages": result, "ratio": ratio},
                    )
                )
                # prevent GC of fire-and-forget task (RUF006)
                task.add_done_callback(lambda t: None)
            except RuntimeError:
                # No event loop running (shouldn't happen in async context)
                _logger.debug("Cannot emit pre_compaction event: no event loop")

        # Above emergency threshold — force truncation
        if ratio >= self._config.emergency_threshold:
            result = self._emergency_truncate(result)
            _logger.warning("Emergency truncation applied: %.1f%% usage", ratio * 100)

        return result

    def _emergency_truncate(self, messages: list[Any]) -> list[Any]:
        """Force-truncate oldest messages to get under emergency threshold.

        Always preserves the most recent messages.
        """
        target_tokens = int(self._config.max_tokens * self._config.compact_threshold)

        # Build from newest to oldest until we hit budget
        kept: list[Any] = []
        accumulated = 0
        for msg in reversed(messages):
            msg_tokens = self.estimate_tokens(_msg_content_str(msg))
            if accumulated + msg_tokens <= target_tokens:
                kept.append(msg)
                accumulated += msg_tokens
            else:
                break

        kept.reverse()
        return kept
