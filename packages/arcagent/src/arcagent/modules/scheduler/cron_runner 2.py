"""CronRunner — cron-session spawn with self-scheduling prevention and platform delivery.

T1.12 + T1.13 (SPEC-018 §3.4).

Responsibilities
----------------
1. Strip the ``[SILENT]`` marker from the prompt before the agent sees it.
2. Invoke the agent via ``agent_run_fn`` with ``CRON_AGENT_KWARGS`` so the
   cronjob / messaging / clarify toolsets are removed at the tool-registry
   layer for that session.  The model cannot emit ``schedule_create`` because
   the tool does not exist in that session's manifest.
3. Wrap the agent output in a header: ``[Scheduled task: {name} | ran at {ts}]``.
4. Deliver the wrapped output via ``DeliverySender.send()`` when ``deliver_to``
   is set, respecting ``silent_on_success``.
5. Emit structured audit events for every significant state transition.

Design notes
------------
* ``CRON_AGENT_KWARGS`` is verbatim from SDD §3.4.  It is applied at the
  **tool-registry layer**, not as a policy flag.  The agent callback receives
  these kwargs and MUST honour them before building its tool manifest.
  This is the Hermes pattern for self-scheduling prevention (ASI02).
* ``CronRunner`` owns NO state beyond its constructor arguments.  It is safe
  to call ``run_job()`` concurrently from multiple cron ticks.
* All exceptions are caught and converted to ``CronRunResult(success=False)``.
  Callers never see a raw exception from ``run_job()``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.scheduler.delivery import DeliverySender
from arcagent.modules.scheduler.models import SILENT_MARKER, CronJob, CronRunResult

_logger = logging.getLogger("arcagent.scheduler.cron_runner")

# ---------------------------------------------------------------------------
# Self-scheduling prevention kwargs (SDD §3.4 verbatim — tool-registry-layer
# enforcement, NOT a policy flag).
# ---------------------------------------------------------------------------
CRON_AGENT_KWARGS: dict[str, Any] = {
    "disabled_toolsets": ["cronjob", "messaging", "clarify"],
    "quiet_mode": True,
    "skip_context_files": True,
    "skip_memory": True,
}

# Header template injected around the agent's output before delivery.
_DELIVERY_HEADER = "[Scheduled task: {name} | ran at {ts}]\n{content}"


class CronRunner:
    """Execute a single CronJob in a restricted agent session.

    Args:
        agent_run_fn: Async callable that drives the agent loop.  Must accept
            a positional ``prompt: str`` argument and arbitrary keyword
            arguments (passes ``CRON_AGENT_KWARGS`` as kwargs).  Returns an
            object whose ``.content`` attribute (or ``str(result)``) is the
            agent's output text.
        telemetry: Used for structured audit events.
        delivery_sender: Optional ``DeliverySender`` implementation.  When
            ``None``, delivery silently no-ops (useful when arcgateway is not
            configured).
    """

    def __init__(
        self,
        *,
        agent_run_fn: Callable[..., Awaitable[Any]],
        telemetry: AgentTelemetry,
        delivery_sender: DeliverySender | None = None,
    ) -> None:
        self._agent_run_fn = agent_run_fn
        self._telemetry = telemetry
        self._delivery_sender = delivery_sender

    async def run_job(self, job: CronJob) -> CronRunResult:
        """Execute *job* in a restricted session and optionally deliver output.

        Steps
        -----
        1. Strip ``[SILENT]`` marker → determine ``silent_for_run``.
        2. Call agent with ``CRON_AGENT_KWARGS`` (toolsets removed at registry).
        3. Wrap output in delivery header.
        4. Deliver if ``deliver_to`` is set and conditions are met.
        5. Return ``CronRunResult``.

        This method never raises; all failures become ``success=False`` results.
        """
        ran_at = datetime.now(tz=UTC).isoformat()
        clean_prompt, silent_for_run = self._extract_silent_marker(job)

        self._telemetry.audit_event(
            "cron.session.disabled_tools",
            {
                "job_name": job.name,
                "disabled_toolsets": CRON_AGENT_KWARGS["disabled_toolsets"],
                "silent_for_run": silent_for_run,
                "deliver_to": job.deliver_to,
            },
        )

        result = await self._invoke_agent(job, clean_prompt, ran_at)

        if result.success:
            await self._maybe_deliver(job, result, silent_for_run)
        else:
            # Failures always deliver, regardless of silent flag.
            await self._deliver_on_failure(job, result)

        return CronRunResult(
            job_name=result.job_name,
            content=result.content,
            success=result.success,
            error=result.error,
            ran_at=result.ran_at,
            silent=silent_for_run,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_silent_marker(job: CronJob) -> tuple[str, bool]:
        """Strip [SILENT] from prompt and return (clean_prompt, silent_flag).

        The marker may appear at the very start of the prompt (with optional
        leading whitespace).  Stripping happens BEFORE the agent sees it, so
        the model never receives ``[SILENT]`` as an instruction.

        The ``silent_for_run`` flag is OR-ed with ``job.silent_on_success``
        so that either the config flag or the in-prompt marker can trigger it.
        """
        stripped = job.prompt.lstrip()
        if stripped.startswith(SILENT_MARKER):
            # Remove the marker and any immediately-following whitespace.
            clean = stripped[len(SILENT_MARKER):].lstrip()
            return clean, True

        return job.prompt, job.silent_on_success

    async def _invoke_agent(
        self,
        job: CronJob,
        clean_prompt: str,
        ran_at: str,
    ) -> CronRunResult:
        """Call agent_run_fn with cron kwargs and capture output.

        Returns a CronRunResult (success or failure).  Never raises.
        """
        try:
            raw = await self._agent_run_fn(clean_prompt, **CRON_AGENT_KWARGS)
            content = (getattr(raw, "content", None) or str(raw)) if raw is not None else ""
            return CronRunResult(
                job_name=job.name,
                content=content,
                success=True,
                ran_at=ran_at,
            )
        except Exception as exc:  # intentional catch-all
            _logger.error(
                "CronRunner: job %r failed: %s",
                job.name,
                exc,
                exc_info=True,
            )
            return CronRunResult(
                job_name=job.name,
                content="",
                success=False,
                error=str(exc),
                ran_at=ran_at,
            )

    async def _maybe_deliver(
        self,
        job: CronJob,
        result: CronRunResult,
        silent_for_run: bool,
    ) -> None:
        """Deliver successful result unless silent suppression is active."""
        if not job.deliver_to:
            return

        if silent_for_run:
            self._telemetry.audit_event(
                "cron.skipped_silent",
                {"job_name": job.name, "deliver_to": job.deliver_to},
            )
            _logger.debug(
                "CronRunner: silent suppression active for %r — skipping delivery",
                job.name,
            )
            return

        await self._send(job, result)

    async def _deliver_on_failure(self, job: CronJob, result: CronRunResult) -> None:
        """Always deliver on failure when deliver_to is set."""
        if not job.deliver_to:
            return
        await self._send(job, result)

    async def _send(self, job: CronJob, result: CronRunResult) -> None:
        """Format and dispatch message to delivery_sender.

        Catches all delivery errors and logs them as ``cron.delivery_failed``
        audit events so a flaky platform adapter never crashes the scheduler.
        """
        if self._delivery_sender is None:
            _logger.debug(
                "CronRunner: no delivery_sender configured for job %r — skipping",
                job.name,
            )
            return

        message = _DELIVERY_HEADER.format(
            name=job.name,
            ts=result.ran_at,
            content=result.content if result.success else f"ERROR: {result.error}",
        )

        try:
            await self._delivery_sender.send(job.deliver_to, message)
            self._telemetry.audit_event(
                "cron.delivered",
                {
                    "job_name": job.name,
                    "deliver_to": job.deliver_to,
                    "success": result.success,
                },
            )
            _logger.info(
                "CronRunner: delivered output for job %r to %r",
                job.name,
                job.deliver_to,
            )
        except Exception as exc:  # delivery must not crash scheduler
            self._telemetry.audit_event(
                "cron.delivery_failed",
                {
                    "job_name": job.name,
                    "deliver_to": job.deliver_to,
                    "error": str(exc),
                },
            )
            _logger.error(
                "CronRunner: delivery failed for job %r to %r: %s",
                job.name,
                job.deliver_to,
                exc,
            )
