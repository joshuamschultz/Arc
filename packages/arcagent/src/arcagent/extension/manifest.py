"""SPEC-062 COMP-001 — ``extension.toml``, the only document Arc parses from an extension.

An extension is third-party, lower-trust code, so every claim its manifest makes is
adversarial input. Four controls keep the document from becoming a
privilege-escalation form:

* ``extra="forbid"`` — a typo raises and names the offending key rather than being
  silently ignored (the ``core/module_config.py`` precedent).
* denied keys are **stripped**, not honoured — a manifest cannot reach the vault
  backend, native process tools, the tool preamble, the sandbox path floor, or
  identity key custody (the ``_DENIED_OVERLAY_PATHS`` precedent in
  ``arccli/blueprints.py``).
* an unbounded tool allowlist is refused above personal tier (REQ-268); omitting the
  allowlist is the same unbounded grant as asking for ``*``.
* a third-party artifact is pinned to one exact version **and** one sha256 (REQ-290).
  A floating pin is a supply-chain hole, not a convenience.
* a declared tier floor may only **refuse** to load below that tier — it can never raise
  the deployment's effective stringency (D-579). The operator sets the tier; a bundle
  author may decline to run under it, never redefine it.

Name validation is deliberately absent: :mod:`arcagent.extension.catalog` owns it, and
it is the component that turns a name into a filesystem path.
"""

from __future__ import annotations

import copy
import logging
import tomllib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import Classification
from arcagent.tiers import tier_rank

_logger = logging.getLogger("arcagent.extension.manifest")

#: Trusted-admin-only config paths a manifest must never set. Mirrors
#: ``_DENIED_OVERLAY_PATHS`` (``arccli/blueprints.py``): a lower-trust bundle must not
#: touch the vault backend, native tool execution, the tool preamble, the sandbox
#: filesystem floor (SEC-18), or identity key custody.
_DENIED_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("vault", "backend"),
    ("tools", "process"),
    ("tools", "preamble"),
    ("tools", "policy", "allowed_paths"),
    ("identity", "key_dir"),
)

#: An exact build: starts with a digit, carries no comparator and no wildcard. Rejects
#: ">=2.3", "latest", and "2.*" — each of which lets the executed bytes change later.
_EXACT_VERSION = r"^[0-9][0-9a-zA-Z.+_-]*$"

#: A full sha256 digest, lowercase hex.
_SHA256 = r"^[0-9a-f]{64}$"

#: The allowlist entry that asks for every tool the upstream cares to serve.
_WILDCARD = "*"


def _strip_denied(config: dict[str, Any]) -> dict[str, Any]:
    """Drop trusted-admin-only keys a manifest must not set (see ``_DENIED_CONFIG_PATHS``)."""
    result = copy.deepcopy(config)
    for path in _DENIED_CONFIG_PATHS:
        node: dict[str, Any] | None = result
        for part in path[:-1]:
            nxt = node.get(part) if node is not None else None
            node = nxt if isinstance(nxt, dict) else None
        if node is not None and path[-1] in node:
            node.pop(path[-1])
            _logger.warning(
                "extension manifest set trusted-admin key %s; ignoring it", ".".join(path)
            )
    return result


class _ManifestModel(BaseModel):
    """Base for every manifest table: an unknown key is a hard error, never a shrug."""

    model_config = ConfigDict(extra="forbid")


class ExtensionHeader(_ManifestModel):
    """``[extension]`` — who this bundle is and how it attaches.

    ``attachment`` is an open string on purpose: a third party ships a new attachment
    kind by providing an implementation, and a closed enum here would make that a core
    edit (REQ-278/REQ-280). The loader resolves the kind and refuses an unknown one.
    """

    name: str
    version: str
    attachment: str = Field(min_length=1)
    #: The weakest deployment this bundle agrees to run in. Read ONLY by
    #: :func:`load_manifest`'s refusal, and by nothing that resolves policy —
    #: which is what keeps a bundle author from raising the operator's tier.
    tier_floor: Tier = Tier.PERSONAL


class ArtifactPin(_ManifestModel):
    """``[artifact]`` — the exact third-party build Arc will execute (REQ-290)."""

    package: str
    version: str = Field(pattern=_EXACT_VERSION)
    sha256: str = Field(pattern=_SHA256)


class HostRequirement(_ManifestModel):
    """``[[host_requires]]`` — a prerequisite the operator installs on the host (REQ-262)."""

    name: str
    minimum_version: str | None = None
    instruction: str = ""


class SecretRequirement(_ManifestModel):
    """``[[secrets]]`` — a credential the operator supplies, and under which key."""

    name: str
    prompt: str = ""


class DeclaredTool(_ManifestModel):
    """``[[tools.declared]]`` — one tool's classification and trifecta capability tags."""

    name: str
    description: str = ""
    classification: Classification = "state_modifying"
    capability_tags: list[str] = Field(default_factory=list)


class ToolPolicy(_ManifestModel):
    """``[tools]`` — which tools may register, and what each one is."""

    allow: list[str] | None = None
    declared: list[DeclaredTool] = Field(default_factory=list)

    @property
    def is_unbounded(self) -> bool:
        """True when the manifest declares no bound on which tools may register.

        An omitted ``allow`` is the same unbounded grant as ``["*"]``: whatever the
        upstream serves, registers. The refusal must not be escapable by silence.
        """
        return self.allow is None or _WILDCARD in self.allow


class ApprovalPolicy(_ManifestModel):
    """``[approval]`` — the default human-approval mode for this extension (REQ-274).

    ``outbound`` admits inbound reads and gates every outbound call; an operator may
    relax it to ``none`` for a named connected account, or tighten it to ``all``.
    """

    default: Literal["none", "outbound", "all"] = "outbound"


class ExtensionManifest(_ManifestModel):
    """A parsed, denied-key-stripped ``extension.toml``."""

    extension: ExtensionHeader
    artifact: ArtifactPin | None = None
    host_requires: list[HostRequirement] = Field(default_factory=list)
    secrets: list[SecretRequirement] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def _drop_denied_keys(cls, config: dict[str, Any]) -> dict[str, Any]:
        return _strip_denied(config)


def load_manifest(text: str, *, tier: Tier) -> ExtensionManifest:
    """Parse ``extension.toml`` and apply the tier-gated refusals.

    Args:
        text: The manifest source.
        tier: The deployment tier the extension would load into. Returned
            unchanged in effect — nothing here can strengthen it (D-579).

    Returns:
        The validated manifest.

    Raises:
        ValidationError: An unknown key, a missing table, or an unpinned artifact.
        ExtensionError: The manifest asks for an unbounded tool allowlist above
            personal tier (REQ-268), or declares a tier floor above ``tier``.
    """
    manifest = ExtensionManifest.model_validate(tomllib.loads(text))
    floor = manifest.extension.tier_floor
    if tier_rank(tier) < tier_rank(floor):
        raise ExtensionError(
            code="EXTENSION_REFUSED",
            message=(
                f"extension '{manifest.extension.name}' declares a {floor} tier floor "
                f"and will not load at {tier} tier"
            ),
            details={"reason": "tier_floor", "tier": str(tier), "tier_floor": str(floor)},
        )
    if manifest.tools.is_unbounded and tier is not Tier.PERSONAL:
        raise ExtensionError(
            code="EXTENSION_REFUSED",
            message=(
                f"extension '{manifest.extension.name}' requests an unbounded tool "
                f"allowlist, which is refused at {tier} tier"
            ),
            details={"reason": "unbounded_tool_allowlist", "tier": str(tier)},
        )
    return manifest


__all__ = [
    "ApprovalPolicy",
    "ArtifactPin",
    "DeclaredTool",
    "ExtensionHeader",
    "ExtensionManifest",
    "HostRequirement",
    "SecretRequirement",
    "ToolPolicy",
    "load_manifest",
]
