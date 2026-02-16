"""CRUD tools for the scheduler module — SPEC-002.

Creates 4 RegisteredTool instances that the LLM can call to manage schedules.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import (
    ScheduleEntry,
    generate_schedule_id,
    validate_prompt,
)
from arcagent.modules.scheduler.store import ScheduleStore

_logger = logging.getLogger("arcagent.scheduler.tools")

# Fields that schedule_update is allowed to modify.
_UPDATABLE_FIELDS = frozenset({
    "prompt", "enabled", "expression", "every_seconds",
    "timeout_seconds", "active_hours",
})


def create_scheduler_tools(
    store: ScheduleStore,
    config: SchedulerConfig,
    telemetry: AgentTelemetry,
) -> list[RegisteredTool]:
    """Create the 4 CRUD tools for schedule management."""

    async def _handle_create(
        type: str = "interval",  # noqa: A002 - matches JSON schema field name
        prompt: str = "",
        expression: str | None = None,
        at: str | None = None,
        every_seconds: int | None = None,
        active_hours: dict[str, Any] | None = None,
        timeout_seconds: int = 300,
        **kwargs: Any,
    ) -> str:
        """Create a new schedule."""
        try:
            # Check quota.
            existing = store.load()
            if len(existing) >= config.max_schedules:
                max_s = config.max_schedules
                return json.dumps({"error": f"Schedule quota exceeded (max {max_s})"})

            # Validate prompt with config-driven max length.
            validate_prompt(prompt, max_length=config.max_prompt_length)

            entry = ScheduleEntry(
                id=generate_schedule_id(),
                type=type,
                prompt=prompt,
                expression=expression,
                at=at,
                every_seconds=every_seconds,
                active_hours=active_hours,
                timeout_seconds=timeout_seconds,
            )
            store.add(entry)
            _logger.info("Created schedule %s (type=%s)", entry.id, type)
            return entry.model_dump_json()
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_list(
        enabled_only: bool = False,
        **kwargs: Any,
    ) -> str:
        """List all schedules."""
        entries = store.load()
        if enabled_only:
            entries = [e for e in entries if e.enabled]
        return json.dumps([e.model_dump() for e in entries])

    async def _handle_update(
        id: str = "",  # noqa: A002 - matches JSON schema field name
        **kwargs: Any,
    ) -> str:
        """Update an existing schedule with allowlisted fields only."""
        try:
            # Only allow explicitly permitted fields.
            updates = {
                k: v for k, v in kwargs.items()
                if v is not None and k in _UPDATABLE_FIELDS
            }
            if not updates:
                return json.dumps({"error": "No updatable fields provided"})
            if "prompt" in updates:
                validate_prompt(updates["prompt"], max_length=config.max_prompt_length)
            updated = store.update(id, updates)
            _logger.info("Updated schedule %s", id)
            return updated.model_dump_json()
        except KeyError:
            return json.dumps({"error": f"Schedule '{id}' not found"})
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def _handle_cancel(
        id: str = "",  # noqa: A002 - matches JSON schema field name
        delete: bool = False,
        **kwargs: Any,
    ) -> str:
        """Cancel (disable) or delete a schedule."""
        try:
            if delete:
                store.remove(id)
                _logger.info("Deleted schedule %s", id)
                return json.dumps({"status": "deleted", "id": id})
            else:
                store.update(id, {"enabled": False})
                _logger.info("Disabled schedule %s", id)
                return json.dumps({"status": "disabled", "id": id})
        except KeyError:
            return json.dumps({"error": f"Schedule '{id}' not found"})

    return [
        RegisteredTool(
            name="schedule_create",
            description="Create a new scheduled task (cron, interval, or one-time)",
            input_schema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["cron", "interval", "once"],
                        "description": "Schedule type",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What the agent should do when triggered",
                    },
                    "expression": {
                        "type": "string",
                        "description": "Cron expression (required for type=cron)",
                    },
                    "at": {
                        "type": "string",
                        "description": "ISO 8601 datetime (required for type=once)",
                    },
                    "every_seconds": {
                        "type": "integer",
                        "description": "Interval in seconds (required for type=interval)",
                    },
                    "active_hours": {
                        "type": "object",
                        "description": "Time window when schedule is active",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "timezone": {"type": "string"},
                        },
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Execution timeout in seconds",
                        "default": 300,
                    },
                },
                "required": ["type", "prompt"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_create,
            timeout_seconds=30,
            source="scheduler",
        ),
        RegisteredTool(
            name="schedule_list",
            description="List all scheduled tasks",
            input_schema={
                "type": "object",
                "properties": {
                    "enabled_only": {
                        "type": "boolean",
                        "description": "Only show enabled schedules",
                        "default": False,
                    },
                },
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_list,
            timeout_seconds=30,
            source="scheduler",
        ),
        RegisteredTool(
            name="schedule_update",
            description="Update an existing scheduled task",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Schedule ID to update",
                    },
                    "prompt": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "expression": {"type": "string"},
                    "every_seconds": {"type": "integer"},
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["id"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_update,
            timeout_seconds=30,
            source="scheduler",
        ),
        RegisteredTool(
            name="schedule_cancel",
            description="Cancel (disable) or delete a scheduled task",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Schedule ID to cancel",
                    },
                    "delete": {
                        "type": "boolean",
                        "description": "If true, permanently delete instead of disable",
                        "default": False,
                    },
                },
                "required": ["id"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_cancel,
            timeout_seconds=30,
            source="scheduler",
        ),
    ]
