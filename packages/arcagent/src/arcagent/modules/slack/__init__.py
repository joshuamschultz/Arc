"""Slack messaging module — bidirectional human-agent interaction via Slack Socket Mode.

Exposed to the agent through the decorator-form capabilities in
:mod:`arcagent.modules.slack.capabilities`; per-agent runtime state
(the :class:`~arcagent.modules.slack.bot.SlackBot`, config, telemetry)
lives in :mod:`arcagent.modules.slack._runtime`.
"""
