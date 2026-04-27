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
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import re
import tarfile
import tempfile
from pathlib import Path
from typing import NamedTuple

from arcskill.hub.config import HubConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class Finding(NamedTuple):
    """One scanner finding.

    Attributes
    ----------
    severity:
        ``"critical"``, ``"high"``, ``"medium"``, or ``"low"``.
    category:
        One of the 8 Hermes categories + ``"text_injection"``.
    rule_id:
        Short rule identifier (e.g. ``"curl_pipe_shell"``).
    message:
        Human-readable explanation.
    path:
        File path within the bundle where found (empty for archive-level).
    line:
        Line number within *path* (0 if unknown).
    """

    severity: str
    category: str
    rule_id: str
    message: str
    path: str
    line: int


# ---------------------------------------------------------------------------
# ScanResult
# ---------------------------------------------------------------------------


class ScanResult(NamedTuple):
    """Aggregated scanner output.

    Attributes
    ----------
    verdict:
        ``"safe"``, ``"caution"``, or ``"dangerous"``.
    findings:
        All individual findings (sorted by severity descending).
    counts:
        ``{severity: count}`` mapping.
    scanner_passes:
        List of scanner passes that ran (e.g. ``["regex", "bandit"]``).
    """

    verdict: str
    findings: list[Finding]
    counts: dict[str, int]
    scanner_passes: list[str]


# ---------------------------------------------------------------------------
# Regex bank -- 8 Hermes categories
# ---------------------------------------------------------------------------

# Each entry: (rule_id, category, severity, compiled_regex, message_template)
# The regex is matched against each line of each scanned file.

_REGEX_BANK: list[tuple[str, str, str, re.Pattern[str], str]] = []


def _r(
    rule_id: str,
    category: str,
    severity: str,
    pattern: str,
    message: str,
) -> None:
    """Register one regex rule into the bank."""
    _REGEX_BANK.append(
        (rule_id, category, severity, re.compile(pattern, re.IGNORECASE), message)
    )


# --- Category 1: Exfiltration -----------------------------------------------
_r(
    "remote_fetch",
    "exfiltration",
    "critical",
    r"\b(requests\.get|httpx\.get|urllib\.request\.urlopen|aiohttp\.ClientSession)\b.*http",
    "Skill makes outbound HTTP GET -- potential data exfiltration",
)
_r(
    "dns_exfil",
    "exfiltration",
    "high",
    r"socket\.(gethostbyname|getaddrinfo|create_connection)",
    "DNS lookup or raw socket -- possible exfiltration channel",
)
_r(
    "env_exfil",
    "exfiltration",
    "high",
    r"os\.environ|os\.getenv\(",
    "Environment variable access -- possible credential/secret exfiltration",
)

# --- Category 2: Prompt injection -------------------------------------------
_r(
    "pi_ignore_instructions",
    "prompt_injection",
    "critical",
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    "Prompt injection: 'ignore previous instructions' pattern detected",
)
_r(
    "pi_jailbreak",
    "prompt_injection",
    "critical",
    r"(system\s+prompt|you\s+are\s+now|disregard\s+your|forget\s+your\s+training)",
    "Prompt injection: jailbreak attempt pattern detected",
)
_r(
    "pi_role_override",
    "prompt_injection",
    "high",
    r"(act\s+as\s+a|pretend\s+you\s+are|you\s+are\s+an?\s+AI\s+without)",
    "Prompt injection: role override pattern detected",
)
_r(
    "pi_instruction_tag",
    "prompt_injection",
    "high",
    r"<\s*(instructions?|system|assistant|human)\s*>",
    "Prompt injection: XML instruction tag pattern in text field",
)
_r(
    "pi_reveal_prompt",
    "prompt_injection",
    "high",
    r"(reveal\s+your\s+(system\s+)?prompt|print\s+your\s+(system\s+)?instructions?)",
    "Prompt injection: prompt exfiltration attempt detected",
)

# --- Category 3: Destructive operations -------------------------------------
_r(
    "destructive_rm_rf",
    "destructive",
    "critical",
    r"(shutil\.rmtree|os\.remove|os\.unlink|pathlib.*\.unlink)\(",
    "Destructive file operation: rmtree/unlink usage",
)
_r(
    "destructive_subprocess",
    "destructive",
    "critical",
    r"subprocess\.(run|call|Popen|check_call|check_output)\s*\(\s*['\"]rm\s+-rf",
    "Destructive subprocess: rm -rf via subprocess",
)

# --- Category 4: Persistence ------------------------------------------------
_r(
    "covert_cron",
    "persistence",
    "high",
    r"(crontab|/etc/cron\.(d|daily|hourly|weekly)|at\s+now)",
    "Persistence: cron/at job registration attempt",
)
_r(
    "covert_startup",
    "persistence",
    "high",
    r"(~/.bashrc|~/.profile|~/.zshrc|/etc/rc\.local|LaunchAgents|systemd\s+enable)",
    "Persistence: shell init or startup script modification",
)

# --- Category 5: Network / reverse shell ------------------------------------
_r(
    "curl_pipe_shell",
    "network_reverse",
    "critical",
    r"curl\s+.*\|.*\b(bash|sh|python|perl|ruby)\b",
    "ClawHavoc pattern: curl | bash -- remote code execution via pipe-to-shell",
)
_r(
    "wget_pipe_shell",
    "network_reverse",
    "critical",
    r"wget\s+.*\|.*\b(bash|sh|python)\b",
    "Remote code execution: wget | shell pipe pattern",
)
_r(
    "nc_reverse_shell",
    "network_reverse",
    "critical",
    r"\bnc\b.*(-e\s+/bin|/dev/tcp/|exec\s+/bin/bash)",
    "Reverse shell: netcat execution pattern",
)
_r(
    "python_socket_shell",
    "network_reverse",
    "high",
    r"import\s+socket.*\bexec\b",
    "Potential socket-based reverse shell",
)

# --- Category 6: Obfuscation ------------------------------------------------
_r(
    "base64_exec",
    "obfuscation",
    "high",
    r"(base64\.b64decode|binascii\.a2b_base64)\s*\(.*\)",
    "Obfuscation: base64-decoded content may hide malicious payload",
)
_r(
    "eval_exec",
    "obfuscation",
    "critical",
    r"\b(eval|exec)\s*\(",
    "Obfuscation: eval/exec call -- dynamic code execution",
)
_r(
    "compile_dynamic",
    "obfuscation",
    "high",
    r"\bcompile\s*\(",
    "Obfuscation: compile() call -- dynamic code compilation",
)
_r(
    "unicode_invisible",
    "obfuscation",
    "high",
    r"[\u200b\u200c\u200d\u2060\ufeff]",
    "Obfuscation: invisible Unicode characters detected",
)

# --- Category 7: Credential leak --------------------------------------------
_r(
    "hardcoded_api_key",
    "credential_leak",
    "critical",
    r"""(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*=\s*['"][A-Za-z0-9_/+=-]{16,}['"]""",
    "Hardcoded credential: API key or secret token literal",
)
_r(
    "hardcoded_password",
    "credential_leak",
    "high",
    r"""(?i)password\s*=\s*['"][^'"]{6,}['"]""",
    "Hardcoded credential: password literal",
)
_r(
    "aws_key",
    "credential_leak",
    "critical",
    r"(AKIA|ASIA|AROA)[A-Z0-9]{16}",
    "AWS access key pattern detected",
)

# --- Category 8: Structural (agent-config write) ----------------------------
# SDD §3.8 Attack #2: covert agent-config persistence.
# Patterns detect write operations (write_text, write_bytes, open with w/a mode)
# but NOT read operations (open with r mode) to avoid false positives.
_r(
    "write_claude_md",
    "structural",
    "critical",
    r"""(CLAUDE\.md.*\.(write_text|write_bytes)|(open)\s*\([^)]*CLAUDE\.md[^)]*['"][waxWAX])""",
    "Covert config write: attempt to write CLAUDE.md (ASI06 attack vector)",
)
_r(
    "write_agents_md",
    "structural",
    "critical",
    r"""(AGENTS\.md.*\.(write_text|write_bytes)|(open)\s*\([^)]*AGENTS\.md[^)]*['"][waxWAX])""",
    "Covert config write: attempt to write AGENTS.md (ASI06 attack vector)",
)
_r(
    "write_identity_md",
    "structural",
    "critical",
    r"""(identity\.md.*\.(write_text|write_bytes)|(open)\s*\([^)]*identity\.md[^)]*['"][waxWAX])""",
    "Covert config write: attempt to write identity.md (ASI06 attack vector)",
)
_r(
    "write_policy_dir",
    "structural",
    "critical",
    r"""(policy/.*\.(write_text|write_bytes)|(open)\s*\([^)]*policy/[^)]*['"][waxWAX])""",
    "Covert config write: attempt to write to policy/ directory (ASI06)",
)
_r(
    "dynamic_import",
    "structural",
    "high",
    r"__import__\s*\(",
    "Dynamic __import__ call -- potential supply-chain bypass",
)
_r(
    "importlib_dynamic",
    "structural",
    "high",
    r"importlib\.(import_module|util\.spec_from_file_location)",
    "Dynamic importlib usage -- potential supply-chain bypass",
)

# --- Agentic-specific -------------------------------------------------------
_r(
    "agent_goal_override",
    "agentic",
    "critical",
    r"(ignore\s+your\s+(goals?|mission|purpose)|override\s+your\s+policy)",
    "Agentic attack: goal/policy override instruction in text field",
)
_r(
    "agent_tool_abuse",
    "agentic",
    "high",
    r"(call\s+tool|invoke\s+tool|use\s+tool)\s+(without\s+permission|bypassing\s+policy)",
    "Agentic attack: tool bypass instruction in text field",
)


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
# Pass 1: Regex bank
# ---------------------------------------------------------------------------


def _regex_pass(root: Path) -> list[Finding]:
    """Run the regex bank against every text file in *root*."""
    findings: list[Finding] = []

    for path in _iter_text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(path.relative_to(root))
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule_id, category, severity, pattern, message in _REGEX_BANK:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            severity=severity,
                            category=category,
                            rule_id=rule_id,
                            message=message,
                            path=rel,
                            line=lineno,
                        )
                    )
    return findings


# ---------------------------------------------------------------------------
# Pass 2: Text-field injection scan
# ---------------------------------------------------------------------------

# Patterns specifically targeting prompt-injection in metadata/text fields.
_TEXT_INJECTION_PATTERNS: list[tuple[str, str, re.Pattern[str], str]] = [
    (
        "ti_ignore_instructions",
        "critical",
        re.compile(
            r"ignore\s+(all\s+)?(previous|prior|above|your)\s+instructions?",
            re.IGNORECASE,
        ),
        "Text injection: 'ignore instructions' in user-visible metadata",
    ),
    (
        "ti_system_override",
        "critical",
        re.compile(r"(you\s+are\s+now|act\s+as|pretend\s+to\s+be)", re.IGNORECASE),
        "Text injection: persona override in description/README",
    ),
    (
        "ti_curl_bash",
        "critical",
        re.compile(r"curl\s+.*\|\s*(bash|sh|python)", re.IGNORECASE),
        "ClawHavoc ClickFix: curl | bash in prerequisites/README",
    ),
    (
        "ti_base64_payload",
        "high",
        re.compile(r"echo\s+[A-Za-z0-9+/]{20,}={0,2}\s*\|\s*(base64|bash|sh)", re.IGNORECASE),
        "Obfuscated payload: echo base64 | decode | execute in text field",
    ),
    (
        "ti_reveal_system",
        "high",
        re.compile(
            r"(reveal|print|show|output)\s+(your\s+)?(system\s+)?(prompt|instructions?)",
            re.IGNORECASE,
        ),
        "Text injection: prompt exfiltration instruction in metadata",
    ),
]

# Filenames that should be scanned for text-injection (user-visible fields).
_TEXT_SCAN_NAMES = {
    "readme.md",
    "readme.txt",
    "readme",
    "skill.md",
    "skill.yaml",
    "skill.yml",
    "description.md",
    "description.txt",
    "manifest.yaml",
    "manifest.yml",
    "module.yaml",
    "module.yml",
    "pyproject.toml",
    "setup.cfg",
}


def _text_injection_pass(root: Path, config: HubConfig) -> list[Finding]:
    """Scan user-visible text fields for prompt-injection patterns."""
    findings: list[Finding] = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() not in _TEXT_SCAN_NAMES:
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(path.relative_to(root))
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule_id, severity, pattern, message in _TEXT_INJECTION_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            severity=severity,
                            category="text_injection",
                            rule_id=rule_id,
                            message=message,
                            path=rel,
                            line=lineno,
                        )
                    )

    # Also scan the description field extracted from SKILL.md / module.yaml.
    desc_findings = _scan_manifest_description(root)
    findings.extend(desc_findings)

    return findings


def _scan_manifest_description(root: Path) -> list[Finding]:
    """Extract the 'description' field from SKILL.md / MODULE.yaml and scan it."""
    findings: list[Finding] = []

    for path in root.rglob("*.yaml"):
        try:
            import yaml  # type: ignore[import-untyped]

            data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: S112
            continue

        if not isinstance(data, dict):
            continue

        description: str = str(data.get("description", ""))
        if not description:
            continue

        rel = str(path.relative_to(root))
        for rule_id, severity, pattern, message in _TEXT_INJECTION_PATTERNS:
            if pattern.search(description):
                findings.append(
                    Finding(
                        severity=severity,
                        category="text_injection",
                        rule_id=rule_id,
                        message=f"{message} (in description field)",
                        path=rel,
                        line=0,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Pass 3: Custom AST visitor
# ---------------------------------------------------------------------------


class _DangerousImportVisitor(ast.NodeVisitor):
    """Detect dynamic __import__ and importlib calls in Python AST."""

    def __init__(self) -> None:
        self.findings: list[tuple[int, str, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Check for __import__(), importlib.import_module(), and exec/eval."""
        self.generic_visit(node)

        func = node.func
        if isinstance(func, ast.Name):
            if func.id in ("__import__", "eval", "exec", "compile"):
                self.findings.append(
                    (
                        node.lineno,
                        f"ast_{func.id}",
                        f"AST: {func.id}() call -- dynamic execution",
                    )
                )

        elif isinstance(func, ast.Attribute):
            full = f"{_attr_name(func)}"
            if full in (
                "importlib.import_module",
                "importlib.util.spec_from_file_location",
                "importlib.util.module_from_spec",
            ):
                self.findings.append(
                    (
                        node.lineno,
                        "ast_dynamic_import",
                        f"AST: {full}() -- dynamic module loading",
                    )
                )


def _attr_name(node: ast.Attribute | ast.Name) -> str:
    """Reconstruct a dotted attribute name from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attr_name(node.value)}.{node.attr}"  # type: ignore[arg-type]
    return "<unknown>"


def _ast_pass(root: Path) -> list[Finding]:
    """Run the custom AST visitor on all Python files."""
    findings: list[Finding] = []

    for path in root.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, OSError):
            continue

        visitor = _DangerousImportVisitor()
        visitor.visit(tree)
        rel = str(path.relative_to(root))

        for lineno, rule_id, message in visitor.findings:
            findings.append(
                Finding(
                    severity="high",
                    category="structural",
                    rule_id=rule_id,
                    message=message,
                    path=rel,
                    line=lineno,
                )
            )

    return findings


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


def _iter_text_files(root: Path) -> list[Path]:
    """Return all likely text files under *root*, excluding binary formats."""
    text_suffixes = {
        ".py",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".ini",
        ".sh",
        ".bash",
        ".zsh",
        ".json",
        ".js",
        ".ts",
        ".html",
        ".rst",
    }
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in text_suffixes
    ]


def _is_available(tool: str) -> bool:
    """Return True if *tool* is importable as a Python module."""
    return importlib.util.find_spec(tool) is not None


def regex_bank_size() -> int:
    """Return the number of compiled regex rules in the bank."""
    return len(_REGEX_BANK)
