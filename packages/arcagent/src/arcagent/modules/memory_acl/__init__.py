"""memory_acl — Module Bus ACL guard for memory operations.

Public surface:
- MemoryACLModule: the bus subscriber class
- Capability: per-turn signed grant model
- CapabilityStore: issues, verifies, revokes capabilities

Usage:
    from arcagent.modules.memory_acl import MemoryACLModule, Capability, CapabilityStore
"""

from arcagent.modules.memory_acl.capability_tokens import Capability, CapabilityStore
from arcagent.modules.memory_acl.errors import ACLViolation, CapabilityExpired, CapabilityInvalid
from arcagent.modules.memory_acl.memory_acl_module import MemoryACLModule

__all__ = [
    "ACLViolation",
    "Capability",
    "CapabilityExpired",
    "CapabilityInvalid",
    "CapabilityStore",
    "MemoryACLModule",
]
