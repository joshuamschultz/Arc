"""Regex-based scanner passes — Hermes 8 categories + text-injection.

Sibling of ``arcskill.hub.scanner``. Owns the static regex banks and the
two passes that consume them: line-level source scanning (`_regex_pass`)
and metadata text-injection scanning (`_text_injection_pass` +
`_scan_manifest_description`).

Re-exported through ``arcskill.hub.scanner`` so callers and tests
continue to do ``from arcskill.hub.scanner import _REGEX_BANK,
_regex_pass`` etc.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from arcskill.hub._findings import Finding
from arcskill.hub.config import HubConfig

logger = logging.getLogger(__name__)


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
    _REGEX_BANK.append((rule_id, category, severity, re.compile(pattern, re.IGNORECASE), message))


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
    r"[​‌‍⁠﻿]",
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
            import yaml

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
# Helper: text-file iteration
# ---------------------------------------------------------------------------


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
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in text_suffixes]
