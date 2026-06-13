"""arcgateway-telegram — Telegram platform adapter plugin for arcgateway.

Install this package alongside ``arcgateway`` and enable ``[platforms.telegram]``
in ``gateway.toml``; the gateway discovers the adapter via the
``arcgateway.adapters`` entry point and routes Telegram DMs to your agent.

    from arcgateway_telegram import PLUGIN, TelegramAdapter, TelegramPlatformConfig
"""

from arcgateway_telegram.adapter import TelegramAdapter, split_message
from arcgateway_telegram.config import TelegramPlatformConfig
from arcgateway_telegram.plugin import PLUGIN, build

__all__ = [
    "PLUGIN",
    "TelegramAdapter",
    "TelegramPlatformConfig",
    "build",
    "split_message",
]

__version__ = "0.1.0"
