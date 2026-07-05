"""Secret-scanning patterns — folded into RegexPiiDetector as the SECRETS category.

Structured-prefix patterns only (AWS/GitHub/JWT/PEM/DB URL). This is a
deliberate scope boundary (SDD Research Insight #4, ADR-423): no
Shannon-entropy generic-secret detection, so a bare high-entropy token
with no recognizable prefix will NOT be caught here. Reference secret
scanners (truffleHog, git-secrets) add an entropy tier for that case;
arcllm keeps a single deterministic detect->redact code path and accepts
the gap rather than adding a second, heavier detection subsystem.
"""

from __future__ import annotations

import re

# (secret_type, compiled pattern). Folded into RegexPiiDetector under the
# single togglable "SECRETS" category (ADR-423) — matches redact as
# [SECRET:TYPE] rather than [PII:TYPE] (namespace differs, code path
# does not).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[posu]_[A-Za-z0-9]{36,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    # Bounded lazy quantifier (was unbounded `[\s\S]*?`) — caps the worst-case
    # scan cost of a single PEM block match rather than letting it run to the
    # end of an arbitrarily large input (LLM10).
    ("PEM_BLOCK", re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]{1,20000}?-----END [A-Z ]+-----")),
    ("DB_URL", re.compile(r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?)://\S+")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{80,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
]
