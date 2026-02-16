"""PolicyModule — Module Bus subscriber for self-learning adaptation policy.

Implements the ACE framework by evaluating agent behavior at configured
intervals and updating policy.md with learned lessons. Injects policy.md
content into the system prompt via ``agent:assemble_prompt``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.policy.config import PolicyConfig
from arcagent.modules.policy.policy_engine import PolicyEngine
from arcagent.utils import load_eval_model

_logger = logging.getLogger("arcagent.modules.policy")

# Maximum background tasks before dropping new ones (backpressure)
_MAX_BACKGROUND_QUEUE = 5

# Default timeout for background eval tasks
_BACKGROUND_TASK_TIMEOUT = 120.0


class PolicyModule:
    """Module Bus subscriber for self-learning adaptation policy.

    Extracted from MarkdownMemoryModule to allow independent
    loading, disabling, and swapping of adaptation strategies.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        eval_config: EvalConfig | None = None,
        telemetry: Any = None,
        workspace: Path = Path("."),
        llm_config: Any | None = None,
    ) -> None:
        self._config = PolicyConfig(**(config or {}))
        self._eval_config = eval_config or EvalConfig()
        self._telemetry = telemetry
        self._workspace = workspace.resolve()
        self._llm_config = llm_config
        self._eval_model: Any = None

        self._engine = PolicyEngine(
            config=self._config,
            workspace=self._workspace,
            telemetry=self._telemetry,
        )

        self._session_messages: list[dict[str, Any]] = []
        self._turn_count: int = 0
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(self._eval_config.max_concurrent)

    @property
    def name(self) -> str:
        return "policy"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register event handlers with the module bus."""
        bus = ctx.bus
        bus.subscribe(
            "agent:post_respond",
            self._on_post_respond,
            priority=110,
            module_name="policy",
        )
        bus.subscribe(
            "agent:assemble_prompt",
            self._on_assemble_prompt,
            priority=60,
            module_name="policy",
        )
        bus.subscribe(
            "agent:shutdown",
            self._on_shutdown,
            priority=60,
            module_name="policy",
        )

    async def shutdown(self) -> None:
        """Cancel background tasks."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def _get_eval_model(self) -> Any:
        """Lazy-init eval model from config, fallback to agent's LLM config.

        Respects ``EvalConfig.fallback_behavior``:
        - ``"skip"``: return None on failure (default)
        - ``"error"``: raise on failure
        """
        if self._eval_model is not None:
            return self._eval_model

        eval_cfg = self._eval_config
        if eval_cfg.provider and eval_cfg.model:
            model_id = f"{eval_cfg.provider}/{eval_cfg.model}"
        elif self._llm_config is not None:
            model_id = self._llm_config.model
        else:
            if eval_cfg.fallback_behavior == "error":
                msg = "No eval model config and no LLM config fallback"
                raise RuntimeError(msg)
            _logger.warning("No eval model config and no LLM config fallback")
            return None

        try:
            self._eval_model = load_eval_model(model_id)
        except Exception:
            if eval_cfg.fallback_behavior == "error":
                raise
            _logger.exception("Failed to load eval model: %s", model_id)
            return None
        return self._eval_model

    async def _on_assemble_prompt(self, ctx: EventContext) -> None:
        """Inject policy.md content into system prompt."""
        sections = ctx.data.get("sections")
        if sections is None or not isinstance(sections, dict):
            return

        policy_path = self._workspace / "policy.md"
        if policy_path.exists():
            content = policy_path.read_text(encoding="utf-8").strip()
            if content:
                sections["policy"] = content

    async def _on_post_respond(self, ctx: EventContext) -> None:
        """Fire periodic policy evaluation."""
        model = self._get_eval_model()
        if model is None:
            return

        messages = ctx.data.get("messages", [])
        if not messages:
            return

        session_id = ctx.data.get("session_id", "")

        # Accumulate messages for session-end policy evaluation
        self._session_messages = messages

        # Periodic evaluation at configured interval
        self._turn_count += 1
        if self._turn_count % self._config.eval_interval_turns == 0:
            self._spawn_background(
                self._safe_evaluate(messages, model, session_id=session_id)
            )

    async def _on_shutdown(self, ctx: EventContext) -> None:
        """Run policy evaluation once at session end."""
        if not self._session_messages:
            return

        model = self._get_eval_model()
        if model is None:
            return

        session_id = ctx.data.get("session_id", "")
        await self._safe_evaluate(
            self._session_messages, model, session_id=session_id
        )

    async def _safe_evaluate(
        self,
        messages: list[dict[str, Any]],
        model: Any,
        *,
        session_id: str = "",
    ) -> None:
        """Evaluate with error handling respecting fallback_behavior."""
        try:
            await self._engine.evaluate(messages, model, session_id=session_id)
        except Exception:
            if self._eval_config.fallback_behavior == "error":
                raise
            _logger.debug("Policy evaluation error, skipping")

    def _spawn_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Fire-and-forget with semaphore, timeout, backpressure, and logging."""
        if len(self._background_tasks) >= _MAX_BACKGROUND_QUEUE:
            _logger.warning(
                "Background task queue full (%d), dropping task",
                _MAX_BACKGROUND_QUEUE,
            )
            coro.close()
            return

        async def _semaphore_wrapped() -> None:
            async with self._semaphore:
                await asyncio.wait_for(coro, timeout=_BACKGROUND_TASK_TIMEOUT)

        task = asyncio.create_task(_semaphore_wrapped())
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task[None]) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc and self._telemetry is not None:
                self._telemetry.audit_event(
                    "policy.background_error",
                    {
                        "error": str(exc),
                        "type": type(exc).__name__,
                    },
                )

        task.add_done_callback(_on_done)
