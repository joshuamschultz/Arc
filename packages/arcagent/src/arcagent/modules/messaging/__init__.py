"""Messaging module — inter-agent communication via ArcTeam.

The live surface is the decorator-form capability set in
:mod:`arcagent.modules.messaging.capabilities` (hooks, tools, and the durable
PUSH inbox loop), wired through :mod:`arcagent.modules.messaging._runtime` and
:mod:`arcagent.modules.messaging._bootstrap`. arcteam owns the messaging; this
package owns *which* identity signs and *which* substrate carries the traffic.
"""

from __future__ import annotations
