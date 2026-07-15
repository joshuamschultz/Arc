"""Run-control module — operator kill switch for live agent runs.

Agents run too long and, since the operator surfaces (arccli, arcui) are separate
processes, an in-process handle registry can't reach a run to stop it. This module
closes that gap: a store-backed cancel request (``arcstore.cancellations``) written
by a surface is observed by the ``@background_task`` watcher in :mod:`.capabilities`,
which resolves the matching live :class:`arcrun.RunHandle` in the agent's tracked-run
map and calls ``cancel(caller_did, reason)`` — a cooperative, attributable stop
(ASI09/ASI10). Per-agent runtime state lives in :mod:`._runtime`.
"""

from __future__ import annotations
