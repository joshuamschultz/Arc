"""What to say, and when to say nothing.

Pure decision layer: no clock, no channel, no I/O. It takes one run event and
returns the single sentence a person should see, or ``None`` — which is the
answer for most events. Sentences are written for someone who does not work on
this system: short words, one idea each, no jargon.

Two payload fields are written by the model rather than by us: the ``title`` on
``dynamic.phase`` and the ``message`` on ``dynamic.log``.

* ``dynamic.log`` is never narrated. It is the script's own running commentary —
  unbounded in volume and entirely at the model's discretion, which is precisely
  the spam vector. Phases are structural: they are the stages of the plan, and
  the plan is finite.
* A phase title is flattened to one short line and always quoted inside a
  sentence we wrote ("Now working on: ..."), so a script cannot use this channel
  to address the operator in its own voice (LLM01 / ASI09).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arcagent.utils.sanitizer import sanitize_text


@dataclass(frozen=True)
class Line:
    """One sentence to send, plus how the sender should pace it.

    ``kind`` groups lines by what produced them. The minimum-gap throttle exists
    to stop ONE chatty source repeating itself, so it applies only between two
    consecutive lines of the same kind: a stage name straight after the plan line
    is news and goes out, a stage name straight after another stage name waits.
    ``urgent`` skips the throttle outright — the things a person would rather be
    interrupted for than miss (a batch result, an ending).

    ``terminal`` means this run's narration is over and its tally should be
    dropped; an empty ``text`` with ``terminal`` set is "reset, say nothing",
    which is how a successful finish is handled: the answer itself is the news.
    """

    text: str
    kind: str = ""
    urgent: bool = False
    terminal: bool = False


# How a run that did not simply succeed is explained. A plain "completed" is
# absent on purpose — the reply lands immediately after it, and announcing the
# end of work one second before delivering it is noise.
_ENDING_LINES = {
    "paused": "I stopped part way through. I can pick this up again later.",
    "budget_exceeded": "I reached the work limit for this job, so I stopped early.",
    "cancelled": "This work was stopped.",
    "failed": "Something went wrong part way through this work.",
}


def line_for(event: str, payload: Mapping[str, Any], *, max_step_chars: int) -> Line | None:
    """The sentence this run event deserves, or ``None`` when it deserves silence.

    Silent by design: the authoring attempts, the rejected drafts, and the
    script's own log lines all pass through here and produce nothing.
    """
    if event == "dynamic.validated":
        return Line("I have a plan for this. Starting now.", kind="plan", urgent=True)
    if event == "dynamic.resumed":
        return Line("Picking this back up where I left off.", kind="plan", urgent=True)
    if event == "dynamic.fallback":
        return Line(
            "That plan did not work, so I will do this the normal way.",
            kind="ending",
            urgent=True,
            terminal=True,
        )
    if event == "dynamic.pin_rejected":
        return Line(
            "My saved plan is out of date, so I will do this the normal way.",
            kind="ending",
            urgent=True,
            terminal=True,
        )
    if event == "dynamic.phase":
        title = short_title(payload.get("title"), max_step_chars)
        return Line(f"Now working on: {title}", kind="phase") if title else None
    if event == "dynamic.completed":
        ending = _ENDING_LINES.get(str(payload.get("status", "")))
        if ending:
            return Line(ending, kind="ending", urgent=True, terminal=True)
        return Line("", kind="ending", terminal=True)
    return None


def heartbeat_line() -> Line:
    """A single reassurance that a long-running plain run is still going.

    Fixed text written by us, never the model — this line only says "not stuck",
    so it carries no run detail a script could speak through (LLM01 / ASI09).
    """
    return Line("Still working on this.", kind="heartbeat")


def fanout_line(count: int) -> Line:
    """A batch of child agents has just been started."""
    if count == 1:
        return Line("Started 1 agent on this step.", kind="fanout")
    return Line(f"Started {count} agents on this step.", kind="fanout")


def fanin_line(finished: int, total: int) -> Line:
    """Every child agent in the announced batch has come back.

    Reported once per batch rather than once per child: on a four-agent fan-out a
    running tally is four near-identical messages in a row, and the last of them
    already carries everything the first three said.
    """
    failed = total - finished
    if total == 1:
        text = "That agent finished." if failed == 0 else "That agent hit a problem."
        return Line(text, kind="fanin", urgent=True)
    if failed == 0:
        return Line(f"All {total} agents finished.", kind="fanin", urgent=True)
    problem = "1 hit a problem" if failed == 1 else f"{failed} hit a problem"
    return Line(f"{finished} of {total} agents finished. {problem}.", kind="fanin", urgent=True)


def short_title(raw: Any, limit: int) -> str:
    """A model-written stage name, cut down to one short quotable line.

    Strips the invisible and control characters an instruction can hide in, folds
    every newline into a space so a title cannot fake extra messages, and clips
    to ``limit``. A non-string (or an empty result) yields "", meaning: say nothing.
    """
    if not isinstance(raw, str):
        return ""
    clean = " ".join(sanitize_text(raw, max_length=limit * 4).split())
    if not clean:
        return ""
    return clean if len(clean) <= limit else clean[: limit - 3].rstrip() + "..."


__all__ = ["Line", "fanin_line", "fanout_line", "heartbeat_line", "line_for", "short_title"]
