"""Pydantic models for the scheduler module — SPEC-002."""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Literal
from zoneinfo import available_timezones

from croniter import croniter
from pydantic import BaseModel, field_validator, model_validator

# Zero-width characters used in Unicode homoglyph attacks.
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")

# Patterns that indicate prompt injection attempts.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bignore\s+previous\b", re.IGNORECASE),
    re.compile(r"\bdisregard\b", re.IGNORECASE),
    re.compile(r"\binstead\b.*\bdo\b", re.IGNORECASE),
    re.compile(r"\bsystem:", re.IGNORECASE),
    re.compile(r"\bassistant:", re.IGNORECASE),
    re.compile(r"\bexfiltrate\b", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        re.IGNORECASE,
    ),
    # Expanded patterns for broader injection coverage.
    re.compile(r"\bforget\b.*\binstructions?\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\boverride\b", re.IGNORECASE),
    re.compile(r"<\|[a-z_]+\|>", re.IGNORECASE),  # role delimiters like <|system|>
    re.compile(r"\bbase64\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+follow\b", re.IGNORECASE),
]

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Default constraints (overridden by SchedulerConfig at runtime).
DEFAULT_MIN_INTERVAL_SECONDS = 60
DEFAULT_MAX_PROMPT_LENGTH = 500
DEFAULT_MAX_TIMEOUT_SECONDS = 3600


def _normalize_text(text: str) -> str:
    """Normalize Unicode and strip zero-width characters.

    NFKC normalization collapses homoglyphs (e.g. full-width Latin)
    to their ASCII equivalents, preventing regex bypass.
    """
    normalized = unicodedata.normalize("NFKC", text)
    return _ZERO_WIDTH_RE.sub("", normalized)


def validate_prompt(prompt: str, *, max_length: int = DEFAULT_MAX_PROMPT_LENGTH) -> bool:
    """Validate a schedule prompt for length and injection patterns.

    Raises ValueError on failure, returns True on success.
    """
    if len(prompt) > max_length:
        msg = f"Prompt exceeds maximum length ({len(prompt)} > {max_length})"
        raise ValueError(msg)
    normalized = _normalize_text(prompt)
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(normalized):
            msg = "Prompt rejected: possible injection pattern detected"
            raise ValueError(msg)
    return True


class ActiveHours(BaseModel):
    """Time window during which a schedule is allowed to fire."""

    start: str  # "HH:MM"
    end: str  # "HH:MM"
    timezone: str = "UTC"

    @field_validator("start", "end")
    @classmethod
    def _validate_time_format(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            msg = f"Invalid time format '{v}', expected HH:MM (24-hour)"
            raise ValueError(msg)
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        if v not in available_timezones():
            msg = f"Unknown timezone '{v}'"
            raise ValueError(msg)
        return v


class ScheduleMetadata(BaseModel):
    """Audit and runtime metadata for a schedule entry."""

    created_by: Literal["agent", "admin", "user", "system"] = "agent"
    created_at: str = ""
    reason: str = ""
    source_session: str = ""
    last_run: str | None = None
    last_result: Literal["ok", "action_taken", "error", None] = None
    run_count: int = 0
    last_duration_seconds: float | None = None
    last_tokens_used: int | None = None
    last_cost_usd: float | None = None
    consecutive_failures: int = 0


class ScheduleEntry(BaseModel):
    """A single schedule entry — the core data model for the scheduler."""

    id: str
    type: Literal["cron", "interval", "once"]
    prompt: str
    enabled: bool = True

    # Type-specific fields.
    expression: str | None = None  # cron
    at: str | None = None  # once (ISO 8601 with timezone)
    every_seconds: int | None = None  # interval

    # Constraints.
    active_hours: ActiveHours | None = None
    timeout_seconds: int = 300

    # Audit.
    metadata: ScheduleMetadata = ScheduleMetadata()

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, v: str) -> str:
        validate_prompt(v)
        return v

    @field_validator("expression")
    @classmethod
    def _validate_cron_expression(cls, v: str | None) -> str | None:
        if v is not None and not croniter.is_valid(v):
            msg = f"Invalid cron expression: '{v}'"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _validate_type_fields(self) -> ScheduleEntry:
        """Ensure type-specific required fields are present and valid."""
        if self.type == "cron" and not self.expression:
            msg = "Cron schedule requires 'expression'"
            raise ValueError(msg)
        if self.type == "once" and not self.at:
            msg = "Once schedule requires 'at' (ISO 8601 datetime)"
            raise ValueError(msg)
        if self.type == "interval":
            if self.every_seconds is None:
                msg = "Interval schedule requires 'every_seconds'"
                raise ValueError(msg)
            if self.every_seconds < DEFAULT_MIN_INTERVAL_SECONDS:
                msg = (
                    f"Interval {self.every_seconds}s is below minimum "
                    f"({DEFAULT_MIN_INTERVAL_SECONDS}s)"
                )
                raise ValueError(msg)
        if self.timeout_seconds > DEFAULT_MAX_TIMEOUT_SECONDS:
            msg = (
                f"Timeout {self.timeout_seconds}s exceeds maximum "
                f"({DEFAULT_MAX_TIMEOUT_SECONDS}s)"
            )
            raise ValueError(msg)
        return self


def generate_schedule_id() -> str:
    """Generate a unique schedule ID."""
    return f"sched_{uuid.uuid4().hex[:12]}"
