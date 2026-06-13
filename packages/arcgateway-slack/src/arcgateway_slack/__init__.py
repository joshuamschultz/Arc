"""arcgateway-slack — Slack (Socket Mode) platform adapter plugin for arcgateway.

from arcgateway_slack import PLUGIN, SlackAdapter, SlackPlatformConfig
"""

from arcgateway_slack.adapter import SlackAdapter, split_message
from arcgateway_slack.config import SlackPlatformConfig
from arcgateway_slack.plugin import PLUGIN, build

__all__ = [
    "PLUGIN",
    "SlackAdapter",
    "SlackPlatformConfig",
    "build",
    "split_message",
]

__version__ = "0.1.0"
