"""Config-driven SkillAdapter selection — the SPEC-044 pluggable-improver seam.

Maps the ``[modules.skills] adapter`` setting to a concrete :class:`SkillAdapter`:

* ``"none"``      → :class:`NullSkillAdapter` (default; improvement off, zero files).
* ``"arcskill"``  → ``arcskill.improver.ArcSkillImprover`` (lazy import; a partial
  install without the improver degrades to NullSkillAdapter with a warning).
* dotted class path → a user-supplied adapter (BYO), signed/allowlisted above personal.

Mirrors :mod:`arcagent.brain.select`: arcagent never imports an improver type at module
load, and a BYO dotted path is refused unless operator-allowlisted above the personal
tier (importing an unverified class-path is arbitrary code execution — ASI04).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcagent.skilladapt.protocol import NullSkillAdapter, SkillAdapter

_logger = logging.getLogger("arcagent.skilladapt.select")


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
    """Return the configured SkillAdapter (fail-safe: any error degrades to Null)."""
    choice = (setting or "none").strip()
    if choice in ("none", "", "null"):
        return NullSkillAdapter()
    if choice == "arcskill":
        adapter = _try_arcskill(
            workspace,
            config=config or {},
            tier=tier,
            llm=llm,
            signer=signer,
            approval_provider=approval_provider,
            eval_runner=eval_runner,
            audit_sink=audit_sink,
            agent_did=agent_did,
            skill_path=skill_path,
        )
        if adapter is not None:
            return adapter
        _logger.warning(
            "skills adapter='arcskill' but arcskill.improver is unavailable; "
            "running improver-less (NullSkillAdapter)"
        )
        return NullSkillAdapter()
    return _load_custom(choice, workspace, tier=tier, allowlist=adapter_allowlist)


def _try_arcskill(
    workspace: Path,
    *,
    config: dict[str, Any],
    tier: str,
    llm: Any,
    signer: Any,
    approval_provider: Any,
    eval_runner: Any,
    audit_sink: Any,
    agent_did: str,
    skill_path: Callable[[str], Path | None] | None,
) -> SkillAdapter | None:
    """Build an ``ArcSkillImprover`` if arcskill.improver is importable, else ``None``."""
    try:
        improver_mod = importlib.import_module("arcskill.improver")
    except ImportError:
        return None
    improver_config = improver_mod.ImproverConfig(**config)
    adapter: SkillAdapter = improver_mod.ArcSkillImprover(
        workspace,
        config=improver_config,
        tier=tier,
        llm=llm,
        signer=signer,
        approval_provider=approval_provider,
        eval_runner=eval_runner,
        audit_sink=audit_sink,
        agent_did=agent_did,
        skill_path=skill_path,
    )
    return adapter


def _load_custom(
    class_path: str, workspace: Path, *, tier: str, allowlist: tuple[str, ...]
) -> SkillAdapter:
    """Import + instantiate a BYO SkillAdapter from a dotted ``module:Class`` path.

    Above the personal tier a BYO class-path must be operator-allowlisted — otherwise
    it is REFUSED before any import, because importing an unverified dotted path is
    arbitrary code execution at startup (ASI04; mirrors the BYO-brain gate).
    """
    if tier != "personal" and class_path not in allowlist:
        raise ValueError(
            f"BYO skills adapter class-path {class_path!r} is not on the operator "
            f"allowlist; refusing to import an unverified class-path at tier {tier!r} "
            f"(fail-closed)"
        )
    module_name, _, attr = class_path.replace(":", ".").rpartition(".")
    if not module_name:
        raise ValueError(f"invalid skills adapter class path: {class_path!r}")
    cls = getattr(importlib.import_module(module_name), attr)
    adapter: SkillAdapter = cls(workspace)
    return adapter


__all__ = ["select_skill_adapter"]
