"""arcgateway-mattermost — Mattermost platform adapter plugin for arcgateway.

Air-gapped DOE/National Lab chat surface (FedRAMP High / IL5 / JWICS).

    from arcgateway_mattermost import PLUGIN, MattermostAdapter, MattermostPlatformConfig
"""

from arcgateway_mattermost.adapter import MattermostAdapter
from arcgateway_mattermost.config import MattermostPlatformConfig
from arcgateway_mattermost.plugin import PLUGIN, build

__all__ = [
    "PLUGIN",
    "MattermostAdapter",
    "MattermostPlatformConfig",
    "build",
]

__version__ = "0.1.0"
