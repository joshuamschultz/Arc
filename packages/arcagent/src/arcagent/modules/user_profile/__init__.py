"""arcagent.modules.user_profile — per-user profile storage.

Public surface:
    ACL                — access control record embedded in YAML frontmatter
    ACLViolation       — raised when a profile operation violates the ACL
    apply_tombstone    — GDPR tombstone: delete profile + redact sessions
    BodyOverflow       — raised when the body would exceed the 2KB cap
    DurableFact        — single append-only fact with provenance metadata
    ProfileNotFound    — raised when no profile exists for a user DID
    ProfileStore       — atomic markdown read/write; 2KB cap enforcement
    TombstoneEvent     — immutable compliance record for GDPR erasure
    UserProfile        — full profile model (YAML frontmatter + markdown body)
    UserProfileConfig  — configuration (profile_dir, body_cap_bytes, etc.)

The read/write/tombstone tools and bus hooks are the decorator-form
capabilities in :mod:`arcagent.modules.user_profile.capabilities`.
"""

from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.errors import ACLViolation, BodyOverflow, ProfileNotFound
from arcagent.modules.user_profile.models import ACL, DurableFact, UserProfile
from arcagent.modules.user_profile.store import ProfileStore
from arcagent.modules.user_profile.tombstone import TombstoneEvent, apply_tombstone

__all__ = [
    "ACL",
    "ACLViolation",
    "BodyOverflow",
    "DurableFact",
    "ProfileNotFound",
    "ProfileStore",
    "TombstoneEvent",
    "UserProfile",
    "UserProfileConfig",
    "apply_tombstone",
]
