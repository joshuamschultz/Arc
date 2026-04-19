"""arcskill.hub -- Skills Hub: signed-install pipeline and revocation management.

This subpackage is **inert unless** ``[skills.hub] enabled = true`` is set
in the agent configuration file.  It ships with arcskill but the install
pipeline, scanner, and lifecycle tasks only activate on explicit opt-in.

Public re-exports
-----------------
The surface below is the stable API.  Internal modules may evolve without
a semver bump; the items listed here are covered by the project's stability
guarantee.

Install pipeline
~~~~~~~~~~~~~~~~
::

    from arcskill.hub import install, uninstall, update

Configuration
~~~~~~~~~~~~~
::

    from arcskill.hub import HubConfig, TierPolicy, HubPolicy, SkillSource

Scanner
~~~~~~~
::

    from arcskill.hub import scan, ScanResult

Revocation / lifecycle
~~~~~~~~~~~~~~~~~~~~~~
::

    from arcskill.hub import check_revocation_on_boot, quarantine_skill

Errors
~~~~~~
::

    from arcskill.hub import (
        HubDisabled, SourceNotAllowed, SignatureInvalid,
        CRLUnreachable, SandboxRequired, ScanVerdictFailed,
        HubLockFileCorrupted,
    )
"""

from arcskill.hub.config import (
    FindingsAllowed,
    HubConfig,
    HubPolicy,
    RevocationConfig,
    SkillSource,
    TierPolicy,
)
from arcskill.hub.dry_run import DryRunResult, run_dry_run
from arcskill.hub.errors import (
    CRLUnreachable,
    HubDisabled,
    HubLockFileCorrupted,
    SandboxRequired,
    ScanVerdictFailed,
    SignatureInvalid,
    SourceNotAllowed,
)
from arcskill.hub.installer import InstallResult, install, uninstall, update
from arcskill.hub.lifecycle import (
    check_revocation_on_boot,
    quarantine_skill,
    should_unload,
    start_crl_refresh_task,
)
from arcskill.hub.scanner import Finding, ScanResult, scan
from arcskill.hub.verify import VerifyResult, verify_bundle

__all__ = [  # noqa: RUF022 -- grouped by category, not alphabetical
    # Config
    "FindingsAllowed",
    "HubConfig",
    "HubPolicy",
    "RevocationConfig",
    "SkillSource",
    "TierPolicy",
    # Installer
    "install",
    "uninstall",
    "update",
    "InstallResult",
    # Scanner
    "scan",
    "ScanResult",
    "Finding",
    # Verify
    "verify_bundle",
    "VerifyResult",
    # Dry run
    "run_dry_run",
    "DryRunResult",
    # Lifecycle
    "check_revocation_on_boot",
    "quarantine_skill",
    "should_unload",
    "start_crl_refresh_task",
    # Errors
    "CRLUnreachable",
    "HubDisabled",
    "HubLockFileCorrupted",
    "SandboxRequired",
    "ScanVerdictFailed",
    "SignatureInvalid",
    "SourceNotAllowed",
]
