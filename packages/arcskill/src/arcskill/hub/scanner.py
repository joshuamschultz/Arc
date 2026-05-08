"""arcskill.hub.scanner -- Multi-layer security scanner for skill bundles.

Architecture
------------
The scanner runs up to four analysis passes on an unpacked skill bundle:

1. **Regex bank** -- Hermes-derived 8-category regex patterns covering the
   most dangerous patterns.  Results are instant and require no external
   tooling.

2. **Semgrep** -- ``p/security-audit`` + ``p/python-security`` rule packs.
   Optional: if semgrep is not installed the pass is skipped with a warning
   (non-federal) or fails closed (federal).

3. **Bandit** -- AST-based Python security linter.  Same availability policy
   as semgrep.

4. **Custom AST visitor** -- detects dynamic ``__import__`` calls and other
   patterns the regex bank cannot reliably catch.

Attack vectors specifically addressed (SDD §3.8 Top 3):
- ``curl_pipe_shell`` / ``remote_fetch`` → CRITICAL auto-block (ClawHavoc).
- Writes to ``CLAUDE.md`` / ``AGENTS.md`` / ``identity.md`` / ``policy/*``
  → CRITICAL auto-block (covert config persistence, ASI06).
- Prompt-injection in description / README / SKILL.md → auto-block at
  federal (no human review).

Verdict enum:
    ``safe``       -- 0 critical, 0 high, findings ≤ policy.max_findings_allowed
    ``caution``    -- non-zero high findings; within policy limits
    ``dangerous``  -- critical findings present OR policy limits exceeded

All user-visible text fields (description, README, SKILL.md body) are scanned
for injection-pattern content, not just source code.

Sibling modules
---------------
- ``arcskill.hub._findings``         — ``Finding`` and ``ScanResult`` types.
- ``arcskill.hub._secret_patterns``  — Regex bank + ``_regex_pass`` +
  ``_text_injection_pass`` + ``_scan_manifest_description``.
- ``arcskill.hub._ast_scanner``      — ``_DangerousImportVisitor`` +
  ``_ast_pass``.

Names from the siblings are re-exported through this module so existing
imports (``from arcskill.hub.scanner import Finding, ScanResult, scan,
_REGEX_BANK, _regex_pass, _ast_pass``) keep working without modification.
"""

from __future__ import annotations

import importlib.util
import logging
import tarfile
import tempfile
from pathlib import Path

from arcskill.hub._ast_scanner import (
    _ast_pass,
    _attr_name,
    _DangerousImportVisitor,
)
from arcskill.hub._findings import Finding, ScanResult
from arcskill.hub._secret_patterns import (
    _REGEX_BANK,
    _TEXT_INJECTION_PATTERNS,
    _TEXT_SCAN_NAMES,
    _iter_text_files,
    _r,
    _regex_pass,
    _scan_manifest_description,
    _text_injection_pass,
)
from arcskill.hub.config import HubConfig

logger = logging.getLogger(__name__)


__all__ = [
    "_REGEX_BANK",
    "_TEXT_INJECTION_PATTERNS",
    "_TEXT_SCAN_NAMES",
    "Finding",
    "ScanResult",
    "_DangerousImportVisitor",
    "_ast_pass",
    "_attr_name",
    "_iter_text_files",
    "_r",
    "_regex_pass",
    "_scan_manifest_description",
    "_text_injection_pass",
    "regex_bank_size",
    "scan",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan(bundle_path: Path, config: HubConfig) -> ScanResult:
    """Run all scanner passes on *bundle_path*.

    The bundle is extracted to a temporary directory; scanning runs there.

    Parameters
    ----------
    bundle_path:
        Path to the ``.tar.gz`` skill bundle.
    config:
        Hub config for tier and policy settings.

    Returns
    -------
    ScanResult
        Aggregated verdict and all findings.
    """
    all_findings: list[Finding] = []
    passes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="arcskill_scan_") as tmpdir:
        extracted = Path(tmpdir) / "skill"
        extracted.mkdir()

        # Extract tarball (security: skip absolute paths and .. traversal).
        _safe_extract(bundle_path, extracted)

        # Pass 1: Regex bank.
        regex_findings = _regex_pass(extracted)
        all_findings.extend(regex_findings)
        passes.append("regex")

        # Pass 2: Text-field injection scan.
        text_findings = _text_injection_pass(extracted, config)
        all_findings.extend(text_findings)
        passes.append("text_injection")

        # Pass 3: Custom AST visitor.
        ast_findings = _ast_pass(extracted)
        all_findings.extend(ast_findings)
        passes.append("ast")

        # Pass 4: Semgrep (optional).
        if _is_available("semgrep"):
            sem_findings = _semgrep_pass(extracted, config)
            all_findings.extend(sem_findings)
            passes.append("semgrep")
        elif config.is_federal:
            logger.warning(
                "semgrep not installed; federal tier should have semgrep available. "
                "Install arcskill[hub] for full scanning."
            )

        # Pass 5: Bandit (optional).
        if _is_available("bandit"):
            bandit_findings = _bandit_pass(extracted, config)
            all_findings.extend(bandit_findings)
            passes.append("bandit")
        elif config.is_federal:
            logger.warning(
                "bandit not installed; federal tier should have bandit available. "
                "Install arcskill[hub] for full scanning."
            )

    verdict = _compute_verdict(all_findings, config)
    counts = _count_by_severity(all_findings)

    return ScanResult(
        verdict=verdict,
        findings=all_findings,
        counts=counts,
        scanner_passes=passes,
    )


# ---------------------------------------------------------------------------
# Pass 4: Semgrep
# ---------------------------------------------------------------------------


def _semgrep_pass(root: Path, config: HubConfig) -> list[Finding]:
    """Run semgrep security-audit rules against the skill directory."""
    import json as _json
    import subprocess

    findings: list[Finding] = []
    cmd = [
        "semgrep",
        "--config=p/security-audit",
        "--config=p/python-security",
        "--json",
        "--quiet",
        str(root),
    ]

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("semgrep failed to run: %s", exc)
        return findings

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return findings

    for match in data.get("results", []):
        severity_map = {"ERROR": "critical", "WARNING": "high", "INFO": "medium"}
        raw_sev: str = match.get("extra", {}).get("severity", "WARNING")
        severity = severity_map.get(raw_sev.upper(), "medium")
        path_str: str = match.get("path", "")
        try:
            rel = str(Path(path_str).relative_to(root))
        except ValueError:
            rel = path_str

        findings.append(
            Finding(
                severity=severity,
                category="semgrep",
                rule_id=match.get("check_id", "semgrep_unknown"),
                message=match.get("extra", {}).get("message", "semgrep finding"),
                path=rel,
                line=match.get("start", {}).get("line", 0),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Pass 5: Bandit
# ---------------------------------------------------------------------------


def _bandit_pass(root: Path, config: HubConfig) -> list[Finding]:
    """Run bandit AST security scan."""
    import json as _json
    import subprocess

    findings: list[Finding] = []
    cmd = [
        "bandit",
        "-r",
        str(root),
        "-f",
        "json",
        "-ll",  # only medium+ severity
        "-q",
    ]

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("bandit failed to run: %s", exc)
        return findings

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return findings

    severity_map = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    confidence_map = {"HIGH": "critical", "MEDIUM": "high", "LOW": "medium"}

    for issue in data.get("results", []):
        bandit_sev: str = issue.get("issue_severity", "MEDIUM")
        bandit_conf: str = issue.get("issue_confidence", "MEDIUM")

        # Elevate to critical when both severity and confidence are HIGH.
        if bandit_sev == "HIGH" and bandit_conf == "HIGH":
            severity = "critical"
        elif bandit_sev == "HIGH":
            severity = confidence_map.get(bandit_conf, "high")
        else:
            severity = severity_map.get(bandit_sev, "medium")

        path_str: str = issue.get("filename", "")
        try:
            rel = str(Path(path_str).relative_to(root))
        except ValueError:
            rel = path_str

        findings.append(
            Finding(
                severity=severity,
                category="bandit",
                rule_id=issue.get("test_id", "bandit_unknown"),
                message=issue.get("issue_text", "bandit finding"),
                path=rel,
                line=issue.get("line_number", 0),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------


def _compute_verdict(findings: list[Finding], config: HubConfig) -> str:
    """Compute the final verdict from findings and policy.

    Rules:
    - Any critical finding → ``"dangerous"`` immediately.
    - Findings counts exceeding policy maximums → ``"dangerous"``.
    - Any high findings (within policy) → ``"caution"``.
    - Otherwise → ``"safe"``.
    """
    counts = _count_by_severity(findings)
    policy = config.policy.max_findings_allowed

    if counts.get("critical", 0) > policy.critical:
        return "dangerous"
    if counts.get("high", 0) > policy.high:
        return "dangerous"
    if counts.get("medium", 0) > policy.medium:
        return "dangerous"
    if counts.get("critical", 0) > 0:
        return "dangerous"
    if counts.get("high", 0) > 0:
        return "caution"
    return "safe"


def _count_by_severity(findings: list[Finding]) -> dict[str, int]:
    """Return a ``{severity: count}`` mapping."""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _safe_extract(bundle_path: Path, dest: Path) -> None:
    """Extract a tarball to *dest*, rejecting path-traversal entries."""
    with tarfile.open(bundle_path) as tf:
        for member in tf.getmembers():
            # Reject absolute paths and parent-directory traversal.
            if member.name.startswith("/") or ".." in member.name:
                logger.warning("Skipping suspicious tarball entry: %r", member.name)
                continue
            # filter="data" (PEP 706) blocks symlinks, special files, and
            # path traversal at the tarfile level. Required default in Py3.14+.
            tf.extract(member, path=dest, filter="data")


def _is_available(tool: str) -> bool:
    """Return True if *tool* is importable as a Python module."""
    return importlib.util.find_spec(tool) is not None


def regex_bank_size() -> int:
    """Return the number of compiled regex rules in the bank."""
    return len(_REGEX_BANK)
