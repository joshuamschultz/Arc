"""Pulse engine — timer loop, check selection, and execution.

Reads pulse.md for the check list, maintains pulse-state.json
for timestamps, and calls agent_run_fn with focused prompts.
"""

from __future__ import annotations

import asyncio
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

from arcagent.modules.pulse import PulseCheck, PulseCheckState, PulseConfig, PulseState

if TYPE_CHECKING:
    from arcagent.core.module_bus import ModuleBus

_logger = logging.getLogger("arcagent.pulse")

AgentRunFn = Callable[..., Awaitable[Any]]

# --- Parser regexes ---

_SECTION_RE = re.compile(r"^##\s+(\S+)", re.MULTILINE)
_INTERVAL_RE = re.compile(r"-\s+\*\*Interval:\*\*\s*(\d+)\s*min", re.IGNORECASE)
_ACTION_RE = re.compile(r"-\s+\*\*Action:\*\*\s*(.*)", re.IGNORECASE)


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
            checks.append(
                PulseCheck(
                    name=match.group(1),
                    interval_minutes=int(interval_m.group(1)),
                    action=action,
                )
            )

    return checks


class PulseEngine:
    """Periodic pulse — reads pulse.md, executes all overdue checks."""

    def __init__(
        self,
        workspace: Path,
        config: PulseConfig,
        agent_run_fn: AgentRunFn,
        bus: ModuleBus | None = None,
    ) -> None:
        self._workspace = workspace
        self._config = config
        self._agent_run_fn = agent_run_fn
        self._bus = bus

        self._pulse_file = workspace / config.pulse_file
        self._state_file = workspace / config.state_file

        self._running = False
        self._timer_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
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
        self._timer_task = asyncio.create_task(self._timer_loop())
        _logger.info("Pulse engine started (interval=%ds)", self._config.interval_seconds)

    async def stop(self) -> None:
        """Stop the pulse engine."""
        self._running = False
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
        interval = self._config.interval_seconds

        while self._running:
            try:
                await self._pulse()
                self._consecutive_errors = 0
            except Exception:
                self._consecutive_errors += 1
                _logger.exception(
                    "Pulse error (consecutive: %d)",
                    self._consecutive_errors,
                )
                if self._consecutive_errors >= 5:
                    _logger.critical(
                        "Pulse hit %d consecutive errors, stopping",
                        self._consecutive_errors,
                    )
                    self._running = False
                    return
            await asyncio.sleep(interval)

    async def _pulse(self) -> None:
        """Single pulse cycle: parse checks, execute all overdue."""
        if not self._pulse_file.exists():
            _logger.debug("No pulse.md found at %s", self._pulse_file)
            return

        checks = parse_pulse_file(self._pulse_file.read_text(encoding="utf-8"))
        if not checks:
            self._emit_event("pulse:no_checks", {})
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
            },
        )

        prompt = (
            f'PULSE CHECK: "{check.name}" '
            f"(every {check.interval_minutes} min, last run: {elapsed_str})\n\n"
            f"{check.action}\n\n"
            "Do NOT describe what you would do. Actually call the tools."
        )
        start = time.monotonic()

        try:
            await asyncio.wait_for(
                self._agent_run_fn(prompt, tool_choice={"type": "any"}),
                timeout=self._config.timeout_seconds,
            )
            duration = time.monotonic() - start
            self._update_state(check.name, "ok")
            self._emit_event(
                "pulse:check_completed",
                {
                    "check": check.name,
                    "duration_seconds": round(duration, 2),
                },
            )
            _logger.info("Pulse: '%s' completed in %.1fs", check.name, duration)
        except TimeoutError:
            self._update_state(check.name, "timeout")
            _logger.warning("Pulse: '%s' timed out", check.name)
            self._emit_event(
                "pulse:check_failed",
                {
                    "check": check.name,
                    "error": "timeout",
                },
            )
        except Exception as exc:
            self._update_state(check.name, "error")
            _logger.error("Pulse: '%s' failed: %s", check.name, exc)
            self._emit_event(
                "pulse:check_failed",
                {
                    "check": check.name,
                    "error": str(exc),
                },
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
        except Exception:
            _logger.warning("Failed to parse %s, using empty state", self._state_file)
            return PulseState()

    def _update_state(self, name: str, result: str) -> None:
        """Update pulse-state.json for a completed check (atomic write)."""
        state = self._read_state()
        cs = state.checks.get(name, PulseCheckState())
        cs.consecutive_failures = 0 if result == "ok" else cs.consecutive_failures + 1
        cs.last_run = datetime.now(tz=UTC).isoformat()
        cs.last_result = result
        state.checks[name] = cs

        data = json.dumps(state.model_dump(), indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._state_file.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(self._state_file))
        except Exception:
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
