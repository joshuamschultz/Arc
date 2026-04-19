"""arcskill.hub.config -- Pydantic models for the skills hub TOML configuration.

TOML schema (document for skill authors and operators):

    [skills.hub]
    enabled = false

    [skills.hub.tier]
    level = "federal"                     # "federal" | "enterprise" | "personal"

    [skills.hub.policy]
    require_signature = true
    require_slsa_level = 3                # 0-3; federal minimum is 3
    require_scan_pass = true
    install_path = "cli_only"             # "cli_only" blocks agent-driven install
    [skills.hub.policy.max_findings_allowed]
    critical = 0
    high = 0
    medium = 2

    [[skills.hub.sources]]
    name = "arc-official"
    type = "github"
    repo = "arc-foundation/skills"
    trust = "builtin"
    signer_identity = "https://github.com/arc-foundation/skills/.github/workflows/publish.yml@refs/heads/main"
    signer_issuer = "https://token.actions.githubusercontent.com"

    [[skills.hub.sources]]
    name = "arc-trusted-partners"
    type = "registry"
    url = "https://skills.arcagent.dev/v1/index.json"
    trust = "trusted"
    allowed_publishers = ["anthropics", "openai", "ctg-federal"]
    fulcio_root_ca = "/etc/arc/fulcio.pem"

    [skills.hub.revocation]
    crl_url = "https://skills.arcagent.dev/v1/crl.json"
    crl_refresh_interval_seconds = 3600
    fail_closed_if_unreachable = true
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Tier
# ---------------------------------------------------------------------------


class TierPolicy(BaseModel):
    """Deployment tier controlling install-pipeline strictness."""

    level: Literal["federal", "enterprise", "personal"] = "personal"


# ---------------------------------------------------------------------------
# FindingsAllowed
# ---------------------------------------------------------------------------


class FindingsAllowed(BaseModel):
    """Maximum tolerable scanner findings per severity.

    All counts default to strict-zero so the base config is safe.
    Personal-tier users may increase ``medium`` for lenient policies.
    """

    critical: Annotated[int, Field(ge=0)] = 0
    high: Annotated[int, Field(ge=0)] = 0
    medium: Annotated[int, Field(ge=0)] = 2


# ---------------------------------------------------------------------------
# HubPolicy
# ---------------------------------------------------------------------------


class HubPolicy(BaseModel):
    """Per-install validation requirements.

    Attributes
    ----------
    require_signature:
        Require a valid Sigstore bundle.  Federal always overrides to True.
    require_slsa_level:
        Minimum SLSA Build Level (0-3).  Federal requires 3.
    require_scan_pass:
        Reject installs that exceed ``max_findings_allowed``.
    install_path:
        ``"cli_only"`` blocks any agent-driven install path (D-08).
    max_findings_allowed:
        Per-severity caps enforced by the scanner stage.
    """

    require_signature: bool = True
    require_slsa_level: Annotated[int, Field(ge=0, le=3)] = 3
    require_scan_pass: bool = True
    install_path: Literal["cli_only", "any"] = "cli_only"
    max_findings_allowed: FindingsAllowed = Field(default_factory=FindingsAllowed)


# ---------------------------------------------------------------------------
# SourceTrust
# ---------------------------------------------------------------------------

SourceTrust = Literal["builtin", "trusted", "community", "local"]
"""Trust levels for skill sources (ascending risk).

builtin:   Official Arc Foundation source; ships with the binary.
trusted:   Vetted third-party publisher; requires explicit allowlist entry.
community: Public registry; higher scrutiny required.
local:     Filesystem path; for development and air-gapped installs.
"""


# ---------------------------------------------------------------------------
# SkillSource
# ---------------------------------------------------------------------------


class SkillSource(BaseModel):
    """One entry in ``[[skills.hub.sources]]``.

    Attributes
    ----------
    name:
        Logical name referenced by the operator (e.g. ``"arc-official"``).
    type:
        Transport type: ``"github"`` | ``"registry"`` | ``"wellknown"`` | ``"local"``.
    trust:
        Trust tier for this source.
    repo:
        GitHub ``owner/repo`` -- used when ``type="github"``.
    url:
        HTTP base URL -- used when ``type="registry"`` or ``"wellknown"``.
    path:
        Filesystem path -- used when ``type="local"``.
    signer_identity:
        Expected Fulcio ``certificate-identity`` (full workflow URL at federal).
    signer_issuer:
        Expected Fulcio ``certificate-oidc-issuer``.
    allowed_publishers:
        Publisher allow-list; ``"doe-*"`` glob patterns are supported.
    fulcio_root_ca:
        Path to custom Fulcio root CA PEM (air-gapped deployments).
    """

    name: str
    type: Literal["github", "registry", "wellknown", "local"]
    trust: SourceTrust = "community"
    repo: str | None = None
    url: str | None = None
    path: str | None = None
    signer_identity: str | None = None
    signer_issuer: str | None = None
    allowed_publishers: list[str] = Field(default_factory=list)
    fulcio_root_ca: str | None = None


# ---------------------------------------------------------------------------
# RevocationConfig
# ---------------------------------------------------------------------------


class RevocationConfig(BaseModel):
    """CRL (Certificate Revocation List) configuration.

    Attributes
    ----------
    crl_url:
        URL of the JSON CRL endpoint.  Must return a list of revoked
        skill content_hashes.
    crl_refresh_interval_seconds:
        How often to refresh the local CRL cache.  Default: 1 hour.
    fail_closed_if_unreachable:
        If True and the CRL endpoint is unreachable, install/start is
        hard-blocked.  Federal default is True.
    """

    crl_url: str = "https://skills.arcagent.dev/v1/crl.json"
    crl_refresh_interval_seconds: Annotated[int, Field(ge=60)] = 3600
    fail_closed_if_unreachable: bool = True


# ---------------------------------------------------------------------------
# HubConfig -- root model
# ---------------------------------------------------------------------------


class HubConfig(BaseModel):
    """Root configuration for the Skills Hub.

    Hub is **disabled by default** (D-08 security gate).  The operator must
    explicitly set ``enabled = true`` in ``[skills.hub]``.  No hub code runs
    unless this flag is True.

    Attributes
    ----------
    enabled:
        Master on/off switch.  Default False.
    tier:
        Deployment tier.  Determines which pipeline stages are mandatory.
    policy:
        Per-install validation settings.
    sources:
        Ordered list of skill sources.  At federal, only sources on this
        list are permitted; any unlisted source raises SourceNotAllowed.
    revocation:
        CRL refresh and fail-closed behaviour.
    """

    enabled: bool = False
    tier: TierPolicy = Field(default_factory=TierPolicy)
    policy: HubPolicy = Field(default_factory=HubPolicy)
    sources: list[SkillSource] = Field(default_factory=list)
    revocation: RevocationConfig = Field(default_factory=RevocationConfig)

    @property
    def is_federal(self) -> bool:
        """True when the deployment tier is federal."""
        return self.tier.level == "federal"

    def source_by_name(self, name: str) -> SkillSource | None:
        """Return the SkillSource with the given name, or None."""
        for src in self.sources:
            if src.name == name:
                return src
        return None
