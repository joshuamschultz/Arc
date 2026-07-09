"""Scheduler module — agent self-scheduling with cron, interval, and one-time tasks.

The live surface is the decorator-form capability in :mod:`.capabilities`
(loaded by the capability loader) backed by per-agent runtime state in
:mod:`._runtime`.
"""

from __future__ import annotations
