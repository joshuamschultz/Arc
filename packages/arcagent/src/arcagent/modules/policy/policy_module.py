"""PolicyModule — Module Bus subscriber for self-learning adaptation policy.

Implements the ACE framework by evaluating agent behavior at configured
intervals and updating policy.md with learned lessons. Injects policy.md
content into the system prompt via ``agent:assemble_prompt``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from arcagent.core.config import EvalConfig
from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.policy.config import PolicyConfig
from arcagent.modules.policy.policy_engine import PolicyEngine
from arcagent.utils.model_helpers import get_eval_model, spawn_background

_logger = logging.getLogger("arcagent.modules.policy")


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
        agent_name: str = "",
    ) -> None:
        self._config = PolicyConfig(**(config or {}))
        self._eval_config = eval_config or EvalConfig()
        self._telemetry = telemetry
        self._workspace = workspace.resolve()
        self._llm_config = llm_config
        self._eval_model: Any = None
        self._eval_label = f"{agent_name}/eval" if agent_name else "eval"

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
        """Cancel and await in-flight background tasks before teardown."""
        if self._background_tasks:
            _logger.info(
                "Cancelling %d background task(s) for shutdown",
                len(self._background_tasks),
            )
            for task in self._background_tasks:
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def _get_eval_model(self) -> Any:
        """Lazy-init eval model, caching result."""
        result = get_eval_model(
            cached_model=self._eval_model,
            eval_config=self._eval_config,
            llm_config=self._llm_config,
            logger=_logger,
            agent_label=self._eval_label,
        )
        if result is not None:
            self._eval_model = result
        return result

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
            if self._telemetry is not None:
                self._telemetry.audit_event(
                    "policy.eval_triggered",
                    {"turn": self._turn_count, "session_id": session_id},
                )
            spawn_background(
                self._safe_evaluate(messages, model, session_id=session_id),
                background_tasks=self._background_tasks,
                semaphore=self._semaphore,
                eval_config=self._eval_config,
                telemetry=self._telemetry,
                audit_event_name="policy.background_error",
                logger=_logger,
            )

    async def _on_shutdown(self, ctx: EventContext) -> None:
        """Run policy evaluation once at session end."""
        if not self._session_messages:
            return

        model = self._get_eval_model()
        if model is None:
            return

        session_id = ctx.data.get("session_id", "")
        await self._safe_evaluate(self._session_messages, model, session_id=session_id)

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
            _logger.warning("Policy evaluation error, skipping", exc_info=True)
            if self._telemetry is not None:
                self._telemetry.audit_event(
                    "policy.eval_skipped",
                    {"session_id": session_id, "reason": "evaluation_error"},
                )
