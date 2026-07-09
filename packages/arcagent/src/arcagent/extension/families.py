"""SPEC-047 — the four extension-point families, declared in one place.

Arc has exactly two extension *shapes*; the four roadmap-named families map onto them:

* **select-one** (``brain``, ``skills``) — an :class:`ExtensionPoint` picked by one config
  setting; declared here as a :class:`SelectOneFamily` carrying the point + the config
  dotpath that selects it.
* **scan-many** (``tools``, ``hook-builds``) — a VIEW over the EXISTING SPEC-021
  ``CapabilityRegistry``, filtered by decorator kind. No new loader; inspection only.

DC-3: the registry's fifth kind ``"skill"`` (loaded SKILL.md capability folders) is
deliberately NOT a scan-many view here — the ``skills`` *select-one* family is the
improver **adapter**, a different concept. Loaded skill folders surface via ``arc skill``.
"""

from __future__ import annotations

from dataclasses import dataclass

from arcagent.brain.select import _BRAIN_POINT
from arcagent.extension.point import ExtensionPoint
from arcagent.skilladapt.select import _SKILLADAPT_POINT


@dataclass(frozen=True)
class SelectOneFamily:
    """A select-one family: one :class:`ExtensionPoint` chosen by one config setting."""

    name: str
    point: ExtensionPoint
    setting_path: tuple[str, str]  # (module_name, config_key) e.g. ("memory", "brain")
    allowlist_key: str  # config key holding the operator BYO allowlist


@dataclass(frozen=True)
class ScanManyFamily:
    """A scan-many family: a view over the SPEC-021 registry filtered by decorator kind."""

    name: str
    kinds: frozenset[str]


FAMILIES: tuple[SelectOneFamily | ScanManyFamily, ...] = (
    SelectOneFamily("brain", _BRAIN_POINT, ("memory", "brain"), "brain_allowlist"),
    SelectOneFamily("skills", _SKILLADAPT_POINT, ("skills", "adapter"), "adapter_allowlist"),
    ScanManyFamily("tools", frozenset({"tool"})),
    ScanManyFamily("hook-builds", frozenset({"hook", "background_task", "capability"})),
)


__all__ = ["FAMILIES", "ScanManyFamily", "SelectOneFamily"]
