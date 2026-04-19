"""arcskill.hub.errors -- Hub-specific exception hierarchy.

All hub exceptions inherit from HubError so callers can catch the base
class when they need broad error handling, or a specific subclass when
precise recovery is needed.

Note: HubLockFileCorrupted is defined in arcskill.lock to avoid a
circular import (lock.py is imported by installer.py which is imported
by hub/__init__.py which would import errors.py, closing the cycle).
It is re-exported here for public API consistency.
"""

from __future__ import annotations


class HubError(Exception):
    """Base class for all Skills Hub errors."""


class HubDisabled(HubError):  # noqa: N818
    """Raised when hub functionality is accessed while enabled=false.

    The hub is off by default (D-08 security gate).  Any code path that
    reaches hub logic without a prior ``enabled`` check raises this.
    """


class SourceNotAllowed(HubError):  # noqa: N818
    """Raised when a requested source is not on the configured allowlist.

    Federal tier requires every install source to be explicitly listed in
    ``[[skills.hub.sources]]``.  Personal and enterprise tiers raise this
    when the source name is unrecognised in config.
    """


class SignatureInvalid(HubError):  # noqa: N818
    """Raised when Sigstore / cosign bundle verification fails.

    Covers: Fulcio cert chain check failure, OIDC identity mismatch,
    Rekor inclusion proof failure, or a missing bundle at federal tier.
    """


class SigstoreUnavailable(HubError):  # noqa: N818
    """Raised when the ``sigstore`` package is not installed at federal tier.

    The ``sigstore`` Python package is an optional dependency under
    ``arcskill[hub]``.  When it is absent:

    - **Federal tier**: raises ``SigstoreUnavailable`` immediately.  The
      install hint in the message guides operators to ``pip install
      arcskill[hub]``.
    - **Personal / enterprise tiers**: verification is warn-skipped and a
      ``VerifyResult(skipped=True)`` is returned.

    This is distinct from ``SignatureInvalid`` (which covers cryptographic
    failures) so callers can differentiate "package missing" from
    "bad signature".
    """


class CRLUnreachable(HubError):  # noqa: N818
    """Raised when the CRL endpoint cannot be reached at federal tier.

    Per SDD §3.8: ``fail_closed_if_unreachable = true`` means any network
    error or timeout during CRL fetch converts to this hard failure.  At
    personal / enterprise tiers the caller may catch and warn-skip.
    """


class SandboxRequired(HubError):  # noqa: N818
    """Raised when federal tier requires Firecracker but it is unavailable.

    The dry-run stage MUST use Firecracker microVM isolation at federal.
    If the host has no Firecracker binary or the backend fails to
    initialise, this exception signals that the install cannot proceed.
    """


class ScanVerdictFailed(HubError):  # noqa: N818
    """Raised when the security scanner returns a verdict that blocks install.

    Carries the verdict details so the caller can surface them to the
    operator.

    Attributes
    ----------
    verdict:
        One of ``"caution"`` or ``"dangerous"``.
    findings:
        Human-readable list of finding summaries.
    """

    def __init__(self, verdict: str, findings: list[str]) -> None:
        self.verdict = verdict
        self.findings = findings
        super().__init__(
            f"Scan verdict={verdict!r}; {len(findings)} finding(s): "
            + "; ".join(findings[:3])
            + ("…" if len(findings) > 3 else "")
        )


# Re-exported from arcskill.lock to avoid circular import.
# HubLockFileCorrupted is defined there because lock.py must not import
# from hub/__init__.py (which would create a cycle via installer.py).
from arcskill.lock import HubLockFileCorrupted  # noqa: E402

__all__ = [
    "CRLUnreachable",
    "HubDisabled",
    "HubError",
    "HubLockFileCorrupted",
    "SandboxRequired",
    "ScanVerdictFailed",
    "SignatureInvalid",
    "SigstoreUnavailable",
    "SourceNotAllowed",
]
