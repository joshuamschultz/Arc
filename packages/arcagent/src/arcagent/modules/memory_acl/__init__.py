"""memory_acl — Module Bus ACL guard for memory operations.

The live gate is the three ``@hook`` functions in
:mod:`arcagent.modules.memory_acl.capabilities` (memory.read / write /
search at priority 10), auto-registered by the capability loader and
reading shared state from :mod:`arcagent.modules.memory_acl._runtime`.

Public surface:
- SessionACL: per-session access-control model with frontmatter parsing
- ACLViolation: raised when a memory operation violates the session ACL
"""

from arcagent.modules.memory_acl.acl import SessionACL
from arcagent.modules.memory_acl.errors import ACLViolation

__all__ = [
    "ACLViolation",
    "SessionACL",
]
