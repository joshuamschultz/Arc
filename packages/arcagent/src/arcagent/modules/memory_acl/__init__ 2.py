"""memory_acl — Module Bus ACL guard for memory operations.

Public surface:
- MemoryACLModule: the bus subscriber class
- Capability: per-turn signed grant model
- CapabilityStore: issues, verifies, revokes capabilities

Usage:
    from arcagent.modules.memory_acl import MemoryACLModule, Capability, CapabilityStore
"""

from arcagent.modules.memory_acl.capabilities import Capability, CapabilityStore
from arcagent.modules.memory_acl.memory_acl_module import MemoryACLModule

__all__ = ["Capability", "CapabilityStore", "MemoryACLModule"]
