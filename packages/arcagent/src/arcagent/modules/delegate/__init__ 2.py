"""Delegate module — agent-facing tool wrapping arcrun.spawn().

Implements SDD §3.5 T3.6: arcagent.modules.delegate provides the `delegate`
tool that the agent model can call to spawn focused sub-agents. All security
invariants (DELEGATE_BLOCKED_TOOLS, allowlist intersection, depth cap) are
enforced in this layer before delegating to arcrun.spawn().

Public surface:
    make_delegate_tool()    — factory for the delegate Tool
    DelegateConfig          — tier-driven configuration model
    DELEGATE_BLOCKED_TOOLS  — frozenset of tools always stripped from children
"""

from arcagent.modules.delegate.config import DELEGATE_BLOCKED_TOOLS, DelegateConfig
from arcagent.modules.delegate.delegate_tool import make_delegate_tool

__all__ = [
    "DELEGATE_BLOCKED_TOOLS",
    "DelegateConfig",
    "make_delegate_tool",
]
