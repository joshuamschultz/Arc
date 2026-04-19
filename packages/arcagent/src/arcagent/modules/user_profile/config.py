"""UserProfileConfig — tunable parameters for the user_profile module.

The profile_dir is resolved relative to the agent workspace at startup;
it is not an absolute path in the config so that configs are portable
across environments.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class UserProfileConfig(BaseModel):
    """Configuration for the UserProfile module.

    Attributes:
        profile_dir:      Sub-directory under workspace where per-user
                          profile markdown files are stored.
        body_cap_bytes:   Hard cap on the markdown body (everything after
                          the YAML frontmatter).  Defaults to 2048 (2 KB)
                          per SDD §3.6.
        tombstone_dir:    Sub-directory under workspace where GDPR
                          tombstone records are retained for compliance.
        schema_version:   Current schema version written into every new
                          profile's frontmatter.
    """

    profile_dir: str = "user_profile"
    body_cap_bytes: int = Field(default=2048, ge=256)
    tombstone_dir: str = "tombstone_events"
    schema_version: int = 1

    model_config = {"frozen": True, "extra": "forbid"}
