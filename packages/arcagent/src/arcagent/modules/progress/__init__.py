"""Progress module — live, plain-language narration of a long fan-out run.

A ``dynamic``-strategy turn writes itself a plan, then starts child agents to
carry it out. That can take minutes, and for all of those minutes the person who
asked the question sees nothing at all and cannot tell working from broken.

arcrun already announces every stage of that work on its own event bus. The
arcrun bridge in :mod:`arcagent.core.model_manager` forwards those announcements
onto the module bus as ``agent:run_progress``, with the channel the turn arrived
on stamped on each one. This module turns that stream into a handful of short
sentences — "Started 4 agents on this step.", "3 of 4 agents finished." — and
sends them back to that same channel.

**It sends to the origin channel or to nobody.** There is no remembered channel,
no most-recent channel, no fallback of any kind. A run with no origin (a
schedule, a dispatched task, a headless CLI run) is narrated to nobody, because
the alternative — guessing — is how a message posted in a shared dashboard once
got answered on one person's phone.

Wiring lives in :mod:`.capabilities`, the wording in :mod:`.narrator`, per-agent
state in :mod:`._runtime`. Remove the folder and nothing else changes: runs stay
silent, exactly as they were.
"""

from __future__ import annotations
