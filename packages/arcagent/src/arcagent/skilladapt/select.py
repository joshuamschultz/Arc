"""Config-driven SkillAdapter selection — the SPEC-044 pluggable-improver seam.

A thin :class:`ExtensionPoint` instance over the SPEC-047 generalized ``select_extension``
mechanism. Maps the ``[modules.skills] adapter`` setting to a concrete :class:`SkillAdapter`:

* ``"none"``      → :class:`NullSkillAdapter` (default; improvement off, zero files).
* ``"arcskill"``  → ``arcskill.improver.ArcSkillImprover`` (lazy import; a partial
  install without the improver degrades to NullSkillAdapter with a warning).
* dotted class path → a user-supplied adapter (BYO), instantiated ``cls(workspace)``;
  refused before import above personal unless operator-allowlisted (ASI04).

The choice dispatch, BYO allowlist gate, and dotted-path importer live once in
:func:`arcagent.extension.select.select_extension`; this module only supplies the
arcskill builder and the BYO construction shape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcagent.extension import ExtensionPoint, select_extension
from arcagent.skilladapt.protocol import NullSkillAdapter, SkillAdapter

_logger = logging.getLogger("arcagent.skilladapt.select")


def _build_arcskill(module: Any, context: dict[str, Any]) -> SkillAdapter | None:
    """Build an ``ArcSkillImprover`` from the imported ``arcskill.improver`` module."""
    improver_config = module.ImproverConfig(**context["config"])
    adapter: SkillAdapter = module.ArcSkillImprover(
        context["workspace"],
        config=improver_config,
        tier=context["tier"],
        llm=context["llm"],
        signer=context["signer"],
        approval_provider=context["approval_provider"],
        eval_runner=context["eval_runner"],
        audit_sink=context["audit_sink"],
        agent_did=context["agent_did"],
        skill_path=context["skill_path"],
    )
    return adapter


_SKILLADAPT_POINT = ExtensionPoint(
    name="skills",
    null_factory=NullSkillAdapter,
    builtin_modules={"arcskill": "arcskill.improver"},
    builtin_builder=_build_arcskill,
    byo_constructor=lambda cls, ctx: cls(ctx["workspace"]),
)


def select_skill_adapter(
    setting: str,
    *,
    workspace: Path,
    config: dict[str, Any] | None = None,
    tier: str = "personal",
    llm: Any = None,
    signer: Any = None,
    approval_provider: Any = None,
    eval_runner: Any = None,
    audit_sink: Any = None,
    agent_did: str = "",
    skill_path: Callable[[str], Path | None] | None = None,
    adapter_allowlist: tuple[str, ...] = (),
) -> SkillAdapter:
    """Return the configured SkillAdapter (fail-safe: any degrade path yields Null)."""
    context: dict[str, Any] = {
        "workspace": workspace,
        "config": config or {},
        "tier": tier,
        "llm": llm,
        "signer": signer,
        "approval_provider": approval_provider,
        "eval_runner": eval_runner,
        "audit_sink": audit_sink,
        "agent_did": agent_did,
        "skill_path": skill_path,
    }
    adapter: SkillAdapter = select_extension(
        _SKILLADAPT_POINT,
        setting,
        tier=tier,
        allowlist=tuple(adapter_allowlist),
        context=context,
        logger=_logger,
    )
    return adapter


__all__ = ["select_skill_adapter"]
