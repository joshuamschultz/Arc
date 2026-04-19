"""Nudge submodule — auto-skill-creation nudge trigger.

Thin trigger layer over the existing skill_improver substrate.
Subscribes to agent:post_plan at effective priority 210 (after
trace_collector at 200) and injects an advisory system message
when the trigger conjunction fires.

ASI-09 compliant: never calls skill_manage() itself.
"""

from arcagent.modules.skill_improver.nudge.nudge_emitter import NudgeEmitter
from arcagent.modules.skill_improver.nudge.signals import NudgeSignals

__all__ = [
    "NudgeEmitter",
    "NudgeSignals",
]
