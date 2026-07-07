"""Context Manager — system-prompt assembly + token accounting.

Two responsibilities:

1. Per-turn ``transform_context`` is append-only (keeps the provider cache
   prefix byte-stable). Its only in-turn action is a last-resort emergency
   truncation if a single run reaches the hard ceiling before a compaction
   boundary fires.
2. Boundary helpers — ``compaction_split`` (token-based deep split) and
   ``prune_observations`` (observation masking) — are invoked by
   ``SessionManager.compact`` at a discrete, persisted compaction boundary,
   NOT per turn. Structured LLM summarization lives in the session manager.
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

    async def assemble_system_prompt(
        self,
        workspace: Path,
        extra_sections: dict[str, str] | None = None,
        *,
        query: str = "",
    ) -> str:
        """Build system prompt from workspace files.

        Reads core files (identity.md, context.md), then emits
        agent:assemble_prompt so modules can inject their own sections.
        Caller-supplied extra_sections (e.g. ArcRun strategy guidance)
        are merged after bus handlers but before ordering.

        Final ordering: identity first, context last, rest alphabetically.

        ``workspace/identity.md`` re-read every call (hot-reload contract):
            The file content is read from disk on every invocation, so an
            edit between turns shows up in the next turn's system prompt
            without an agent restart. This is the public contract — see
            ``tests/integration/test_identity_hot_reload.py``. Note this
            is the *content* of identity.md only; the agent's DID and
            keypair are loaded once from ``arcagent.toml [identity]`` at
            ``agent.startup()`` and frozen for the agent's lifetime.
            Changing the DID requires a restart.

        Args:
            workspace: Path to the agent workspace directory.
            extra_sections: Additional named sections to include.
                Merged after bus event handlers run.
            query: The current turn/task text, threaded into the
                ``agent:assemble_prompt`` payload so memory (and any other
                subscriber) can do query-conditioned retrieval. Empty on the
                resume path where no live turn text exists.
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
                {"sections": sections, "workspace": str(workspace), "query": query},
            )

        # Merge caller-supplied sections (strategy guidance, etc.)
        if extra_sections:
            sections.update(extra_sections)

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

    def token_ratio(self) -> float:
        """Calculate ratio from accumulated provider-reported usage."""
        if self._config.max_tokens == 0:
            return 0.0
        return self._reported_input_tokens / self._config.max_tokens

    def message_fill_ratio(self, messages: list[Any]) -> float:
        """Estimated context fill (tokens / max_tokens) for a message list.

        The honest current-context measure used to trigger compaction — reads
        the live messages rather than the reported-usage accumulator.
        """
        return self._estimate_ratio(messages)

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
        """Per-turn context hook for arcrun — append-only by contract.

        Returns messages unchanged so the provider prompt-cache prefix stays
        byte-stable across turns. Compaction (observation masking + a
        structured summary) is a discrete, persisted boundary event owned by
        ``SessionManager.compact`` (via ``maybe_compact``), NOT a per-turn
        rewrite — a per-turn prune of a sliding window would bust the cache on
        every turn of a long-running agent.

        The one exception is a last-resort valve: a single run's ReAct loop can
        add many turns before a compaction boundary fires between dispatches, so
        if in-run context reaches the hard ceiling, drop the oldest messages to
        avoid a provider overflow. Rare and discrete.
        """
        if not messages:
            return messages
        if self._estimate_ratio(messages) >= self._config.emergency_threshold:
            _logger.warning("Emergency truncation: in-run context reached the hard ceiling")
            return self._emergency_truncate(messages)
        return messages

    def compaction_split(self, messages: list[Any]) -> tuple[list[Any], list[Any]]:
        """Split messages into ``(older_to_summarize, recent_to_keep)``.

        Deep + debounced: keeps a recent tail under ~45% of ``max_tokens`` so
        the post-compaction ratio lands near half the window and many
        append-only turns follow before the next boundary (no threshold
        thrash). Keeps >=1 message; summarizes >=1 for any input of >=2 (the
        caller, ``compact``, only invokes this with >=4 messages).
        """
        keep_budget = int(self._config.max_tokens * 0.45)
        kept_tokens = 0
        split_idx = 0
        for i in range(len(messages) - 1, -1, -1):
            kept_tokens += self.estimate_tokens(_msg_content_str(messages[i]))
            if kept_tokens > keep_budget:
                split_idx = i + 1
                break
        split_idx = min(max(split_idx, 1), len(messages) - 1)
        return messages[:split_idx], messages[split_idx:]

    def _emergency_truncate(self, messages: list[Any]) -> list[Any]:
        """Force-truncate oldest messages to get under the emergency threshold.

        Always keeps at least the most recent message — even when that single
        message alone exceeds the budget, returning an empty list would send
        zero messages to the provider (a worse failure than an oversized one).
        """
        target_tokens = int(self._config.max_tokens * self._config.compact_threshold)

        kept: list[Any] = []
        accumulated = 0
        for msg in reversed(messages):
            msg_tokens = self.estimate_tokens(_msg_content_str(msg))
            if kept and accumulated + msg_tokens > target_tokens:
                break
            kept.append(msg)
            accumulated += msg_tokens

        kept.reverse()
        return kept
