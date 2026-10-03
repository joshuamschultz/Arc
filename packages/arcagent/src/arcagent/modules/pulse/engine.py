"""Pulse engine — timer loop, check selection, and execution.

Reads pulse.md for the check list, maintains pulse-state.json
for timestamps, and calls agent_run_fn with focused prompts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arctrust import causal

from arcagent.core.control_contract import (
    ControlArtifactAuthority,
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
    SignedControlRevision,
)
from arcagent.core.run_contract import (
    CanonicalRunRequest,
    RunAdmissionUnavailableError,
    RunOutcomeUnknownError,
    RunTriggerIssuer,
)
from arcagent.modules.pulse import PulseCheck, PulseCheckState, PulseState
from arcagent.modules.pulse.config import PulseConfig
from arcagent.modules.pulse.signed_dispatch import (
    canonical_definition,
    dispatch_signed_pulse,
    is_approved,
)
from arcagent.utils.periodic import FailurePolicy, PeriodicRunner

if TYPE_CHECKING:
    from arcagent.core.module_bus import ModuleBus

_logger = logging.getLogger("arcagent.pulse")

AgentRunFn = Callable[..., Awaitable[Any]]

# Circuit breaker: skip a check after this many consecutive failures
_CHECK_CIRCUIT_BREAKER_THRESHOLD = 5

# --- Parser regexes ---

_SECTION_RE = re.compile(r"^##\s+(\S+)", re.MULTILINE)
_INTERVAL_RE = re.compile(r"-\s+\*\*Interval:\*\*\s*(\d+)\s*min", re.IGNORECASE)
_ACTION_RE = re.compile(r"-\s+\*\*Action:\*\*\s*(.*)", re.IGNORECASE)
_APPROVAL_RE = re.compile(r"-\s+\*\*Approval:\*\*\s*(\{[^\n]+\})", re.IGNORECASE)


def parse_pulse_file(content: str) -> list[PulseCheck]:
    """Parse pulse.md content into a list of PulseCheck objects.

    Expected format per check::

        ## check_name
        - **Interval:** N minutes
        - **Action:** Description of what to do...

    Action text can span multiple lines until the next ``##`` header.
    """
    checks: list[PulseCheck] = []
    section_starts = list(_SECTION_RE.finditer(content))

    for i, match in enumerate(section_starts):
        start = match.end()
        end = section_starts[i + 1].start() if i + 1 < len(section_starts) else len(content)
        body = content[start:end]

        interval_m = _INTERVAL_RE.search(body)
        action_m = _ACTION_RE.search(body)
        if interval_m is None or action_m is None:
            continue

        # Collect action text (first line + continuations)
        lines = body[action_m.start() :].split("\n")
        first = action_m.group(1).strip()
        parts = [first] if first else []
        for line in lines[1:]:
            s = line.strip()
            if not s or s.startswith("- **") or s.startswith("## "):
                break
            parts.append(s)

        action = " ".join(parts)
        if action:
            approval_match = _APPROVAL_RE.search(body)
            try:
                check = PulseCheck(
                    name=match.group(1),
                    interval_minutes=int(interval_m.group(1)),
                    action=action,
                    approval=json.loads(approval_match.group(1)) if approval_match else None,
                )
            except ValueError:
                _logger.warning("Pulse check %s has invalid signed metadata", match.group(1))
                continue
            checks.append(check)

    return checks


class PulseEngine:
    """Periodic pulse — reads pulse.md, executes all overdue checks."""

    def __init__(
        self,
        workspace: Path,
        config: PulseConfig,
        agent_run_fn: AgentRunFn,
        bus: ModuleBus | None = None,
        control_artifact_authority: ControlArtifactAuthority | None = None,
        control_tenant_id: str | None = None,
        agent_did: str = "",
        trigger_issuer: RunTriggerIssuer | None = None,
        prepare_collected_request: Callable[..., CanonicalRunRequest] | None = None,
    ) -> None:
        self._workspace = workspace
        self._config = config
        self._agent_run_fn = agent_run_fn
        self._bus = bus
        self._control_artifact_authority = control_artifact_authority
        self._control_tenant_id = control_tenant_id
        self._agent_did = agent_did
        self._trigger_issuer = trigger_issuer
        self._prepare_collected_request = prepare_collected_request

        self._pulse_file = workspace / config.pulse_file
        self._state_file = workspace / config.state_file

        self._running = False
        self._timer_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._poller = PeriodicRunner()
        self._consecutive_errors = 0
        self._fire_and_forget: set[asyncio.Task[Any]] = set()

    @property
    def running(self) -> bool:
        return self._running

    def set_agent_run_fn(self, fn: AgentRunFn) -> None:
        """Bind or rebind the agent.run() callback."""
        self._agent_run_fn = fn
        self._ready.set()

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the pulse timer loop."""
        self._running = True
        self._poller.reset()
        self._timer_task = asyncio.create_task(self._timer_loop())
        _logger.info("Pulse engine started (interval=%ds)", self._config.interval_seconds)

    async def stop(self) -> None:
        """Stop the pulse engine."""
        self._running = False
        self._poller.stop()
        self._ready.set()

        if self._timer_task is not None:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass

        _logger.info("Pulse engine stopped")

    # --- Core loop ---

    async def _timer_loop(self) -> None:
        """Periodically fire pulse checks."""
        await self._ready.wait()
        if not self._running:
            return

        async def tick() -> None:
            await self._pulse()
            self._consecutive_errors = 0

        def on_error(exc: BaseException, failures: int) -> None:
            self._consecutive_errors = failures
            _logger.error("Pulse error (consecutive: %d): %s", failures, exc)
            if failures >= 5:
                _logger.critical("Pulse hit %d consecutive errors, stopping", failures)

        try:
            await self._poller.run(
                tick,
                interval=self._config.interval_seconds,
                failure=FailurePolicy(max_consecutive=5),
                on_error=on_error,
            )
        finally:
            self._running = False

    async def _pulse(self) -> None:
        """Single pulse cycle: parse checks, execute all overdue."""
        if not self._pulse_file.exists():
            _logger.debug("No pulse.md found at %s", self._pulse_file)
            return

        source = self._pulse_file.read_text(encoding="utf-8")
        checks = parse_pulse_file(source)
        if not checks:
            if _SECTION_RE.search(source):
                _logger.error("Pulse definitions unavailable: no valid checks")
                self._emit_event("pulse:unavailable", {"reason": "invalid_definitions"})
            else:
                self._emit_event("pulse:no_checks", {})
            return

        checks = self._approved_checks(checks)
        if not checks:
            return
        state = self._read_state()
        overdue = self._find_overdue(checks, state)
        if not overdue:
            self._emit_event("pulse:ok", {"checks_evaluated": len(checks)})
            return

        _logger.info("Pulse: %d overdue check(s) to run", len(overdue))
        for check in overdue:
            await self._execute_check(check, state)
            state = self._read_state()

    def _approved_checks(self, checks: list[PulseCheck]) -> list[PulseCheck]:
        """Checks whose approval binds their current text; the rest wait for the operator."""
        runnable: list[PulseCheck] = []
        for check in checks:
            if is_approved(check):
                runnable.append(check)
                continue
            _logger.warning("Pulse: '%s' is awaiting operator approval", check.name)
            self._emit_event(
                "pulse:pending_approval",
                {"check": check.name, "changed": check.approval is not None},
            )
        return runnable

    async def _execute_check(self, check: PulseCheck, state: PulseState) -> None:
        """Execute a single pulse check."""
        elapsed = self._elapsed_minutes(check.name, state)
        elapsed_str = f"{elapsed:.0f} min ago" if elapsed is not None else "never"

        _logger.info("Pulse: running '%s' (last run: %s)", check.name, elapsed_str)
        self._emit_event(
            "pulse:check_started",
            {
                "check": check.name,
                "elapsed_minutes": elapsed,
                "revision": None if check.approval is None else check.approval.revision,
            },
        )

        prompt = (
            f"Scheduled task: {check.name} "
            f"(runs every {check.interval_minutes} min)\n\n"
            f"{check.action}"
        )
        start = time.monotonic()
        current = state.checks.get(check.name, PulseCheckState())
        digest = hashlib.sha256(canonical_definition(check)).hexdigest()
        if current.pending_due_at is not None:
            if current.pending_definition_digest != digest:
                _logger.error("Pulse %s pending definition changed", check.name)
                return
            due_at = datetime.fromisoformat(current.pending_due_at)
        else:
            due_at = datetime.now(UTC)
            current.pending_due_at = due_at.isoformat()
            current.pending_definition_digest = digest
            state.checks[check.name] = current
            self._write_state(state)

        try:
            await asyncio.wait_for(
                self._dispatch(check, prompt, due_at),
                timeout=self._config.timeout_seconds,
            )
            duration = time.monotonic() - start
            self._update_state(check.name, "ok", check.approval)
            self._emit_event(
                "pulse:check_completed",
                {
                    "check": check.name,
                    "duration_seconds": round(duration, 2),
                },
            )
            _logger.info("Pulse: '%s' completed in %.1fs", check.name, duration)
        except TimeoutError:
            _logger.warning("Pulse: '%s' timed out", check.name)
            self._emit_event(
                "pulse:check_failed",
                {
                    "check": check.name,
                    "error": "timeout",
                },
            )
        except (
            ControlArtifactRefusedError,
            ControlArtifactUnavailableError,
            RunAdmissionUnavailableError,
            RunOutcomeUnknownError,
        ) as exc:
            _logger.error("Pulse '%s' remains pending: %s", check.name, exc)
        except Exception as exc:
            _logger.error("Pulse: '%s' failed: %s", check.name, exc)
            self._emit_event(
                "pulse:check_failed",
                {
                    "check": check.name,
                    "error": str(exc),
                },
            )

    async def _dispatch(self, check: PulseCheck, prompt: str, due_at: datetime) -> Any:
        authority = self._control_artifact_authority
        tenant_id = self._control_tenant_id
        issuer = self._trigger_issuer
        prepare = self._prepare_collected_request
        if authority is None or tenant_id is None or issuer is None or prepare is None:
            raise ControlArtifactUnavailableError("signed pulse capability unavailable")
        # Item 20: a pulse firing is caused by its check, on the owning agent's
        # behalf — a fresh root, so nothing bound by the timer's starter leaks in.
        firing = causal.root(
            "scheduler", f"did:arc:pulse:{check.name}", on_behalf_of=self._agent_did or None
        )
        with causal.bind(firing):
            return await dispatch_signed_pulse(
                check,
                prompt=prompt,
                due_at=due_at,
                tenant_id=tenant_id,
                agent_did=self._agent_did,
                authority=authority,
                issuer=issuer,
                prepare=prepare,
                run_fn=self._agent_run_fn,
            )

    # --- Check selection ---

    def _find_overdue(
        self,
        checks: list[PulseCheck],
        state: PulseState,
    ) -> list[PulseCheck]:
        """Find all overdue checks, sorted most overdue first."""
        now = datetime.now(tz=UTC)
        overdue: list[tuple[float, PulseCheck]] = []

        for check in checks:
            cs = state.checks.get(check.name, PulseCheckState())
            if cs.pending_due_at is not None:
                overdue.append((365 * 24 * 3600, check))
                continue
            if cs.consecutive_failures >= _CHECK_CIRCUIT_BREAKER_THRESHOLD:
                _logger.warning(
                    "Pulse: skipping check '%s' - %d consecutive failures (circuit breaker)",
                    check.name,
                    cs.consecutive_failures,
                )
                continue
            if cs.last_run is None:
                overdue.append((365 * 24 * 3600, check))
                continue
            elapsed = (now - datetime.fromisoformat(cs.last_run)).total_seconds()
            gap = elapsed - check.interval_minutes * 60
            if gap > 0:
                overdue.append((gap, check))

        overdue.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in overdue]

    def _elapsed_minutes(self, name: str, state: PulseState) -> float | None:
        """Minutes since last run, or None if never run."""
        cs = state.checks.get(name, PulseCheckState())
        if cs.last_run is None:
            return None
        return (datetime.now(tz=UTC) - datetime.fromisoformat(cs.last_run)).total_seconds() / 60

    # --- State persistence ---

    def _read_state(self) -> PulseState:
        """Read pulse-state.json, returning empty state if missing or corrupt."""
        if not self._state_file.exists():
            return PulseState()
        try:
            return PulseState(**json.loads(self._state_file.read_text(encoding="utf-8")))
        except Exception as exc:
            raise ControlArtifactUnavailableError("pulse progress state unavailable") from exc

    def _update_state(
        self, name: str, result: str, approval: SignedControlRevision | None = None
    ) -> None:
        """Update pulse-state.json for a completed check (atomic write)."""
        state = self._read_state()
        cs = state.checks.get(name, PulseCheckState())
        cs.consecutive_failures = 0 if result == "ok" else cs.consecutive_failures + 1
        cs.last_run = datetime.now(tz=UTC).isoformat()
        cs.last_result = result
        cs.pending_due_at = None
        cs.pending_definition_digest = None
        if approval is not None:
            cs.last_revision = approval.revision
        state.checks[name] = cs

        self._write_state(state)

    def _write_state(self, state: PulseState) -> None:
        """Atomically persist pulse progress in the agent workspace."""

        data = json.dumps(state.model_dump(), indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._state_file.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(self._state_file))
        except Exception:  # reason: re-raise after log
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- Event emission ---

    def _emit_event(self, event: str, data: dict[str, Any]) -> None:
        """Fire-and-forget bus event emission."""
        if self._bus is None:
            return
        task = asyncio.ensure_future(self._bus.emit(event, data))
        self._fire_and_forget.add(task)
        task.add_done_callback(self._fire_and_forget.discard)
