"""Config-driven Brain selection — the SPEC-041 pluggable-brain seam.

A thin :class:`ExtensionPoint` instance over the SPEC-047 generalized ``select_extension``
mechanism. Maps the ``[modules.memory] brain`` setting to a concrete :class:`Brain`,
naming **no** memory backend in arcagent source:

* ``"none"``       → :class:`NullBrain` (default; memory off, zero files).
* a backend name   → that package's ``build_brain(context)`` entrypoint (lazy import —
  arcagent has no static dependency on any memory package; a missing install degrades to
  NullBrain with a warning rather than crashing the agent). Naming an installed memory
  backend runs exactly this generic path: arcagent imports the module the operator named
  and calls its well-known factory, learning nothing about the backend's internals.
* dotted ``module:Class`` path → a user-supplied Brain (BYO), instantiated
  ``cls(workspace, did)``; refused before import above personal unless operator-allowlisted
  (ASI04).

The choice dispatch, BYO allowlist gate, dotted-path importer, and provider entrypoint
call live once in :func:`arcagent.extension.select.select_extension`; this module only
declares the point and assembles the generic context the backend's ``build_brain`` reads.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arcagent.brain.protocol import Brain, NullBrain
from arcagent.extension import ExtensionPoint, select_extension

_logger = logging.getLogger("arcagent.brain.select")

# The well-known factory a memory backend package exposes: ``build_brain(context) -> Brain``.
_PROVIDER_ENTRYPOINT = "build_brain"

_BRAIN_POINT = ExtensionPoint(
    name="brain",
    null_factory=NullBrain,
    provider_entrypoint=_PROVIDER_ENTRYPOINT,
    byo_constructor=lambda cls, ctx: cls(ctx["workspace"], ctx["agent_did"]),
)


def select_brain(
    setting: str,
    *,
    workspace: Path,
    agent_did: str,
    tier: str = "personal",
    audit_sink: Any = None,
    brain_allowlist: tuple[str, ...] = (),
    identity: Any = None,
    policy_pipeline: Any = None,
    backend_config: dict[str, Any] | None = None,
) -> Brain:
    """Return the configured Brain (fail-safe: any degrade path yields NullBrain).

    ``identity`` (the agent's signing key) and ``policy_pipeline`` are threaded to the
    backend so its own writes can be signed and policy-authorized. ``backend_config`` is
    an opaque, backend-defined dict forwarded verbatim from the agent TOML — arcagent does
    not read or name its keys; the selected backend validates them in ``build_brain``.
    """
    context: dict[str, Any] = {
        "workspace": workspace,
        "agent_did": agent_did,
        "tier": tier,
        "audit_sink": audit_sink,
        "identity": identity,
        "policy_pipeline": policy_pipeline,
        "backend_config": backend_config or {},
    }
    brain: Brain = select_extension(
        _BRAIN_POINT,
        setting,
        tier=tier,
        allowlist=tuple(brain_allowlist),
        context=context,
        logger=_logger,
    )
    return brain


__all__ = ["select_brain"]
