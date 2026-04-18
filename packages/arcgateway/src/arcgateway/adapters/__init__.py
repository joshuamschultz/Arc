"""Platform adapter package.

Platform-specific adapters (Telegram, Slack, Discord, etc.) are implemented
in T1.7. This package currently exports only the base Protocol and supporting
types needed for the T1.4 skeleton.

Usage::

    from arcgateway.adapters.base import BasePlatformAdapter, FailedAdapter
"""

from arcgateway.adapters.base import BasePlatformAdapter, FailedAdapter

__all__ = ["BasePlatformAdapter", "FailedAdapter"]
