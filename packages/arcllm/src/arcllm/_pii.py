"""PII detection and redaction — regex-based with pluggable override."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from arcllm._scan_limits import MAX_REGEX_SCAN_LENGTH
from arcllm._secrets import SECRET_PATTERNS
from arcllm.exceptions import ArcLLMConfigError

# Toggle-category name shared by every secret sub-pattern (ADR-423) — one
# detect/redact code path, individually togglable like any PII category.
SECRETS_CATEGORY = "SECRETS"


@dataclass(frozen=True, eq=True)
class PiiMatch:
    """A single PII (or secret) detection result.

    ``namespace`` drives the redaction tag prefix: ``"PII"`` for ordinary
    entities, ``"SECRET"`` for the togglable SECRETS category (ADR-423).
    """

    pii_type: str
    start: int
    end: int
    matched_text: str
    namespace: str = "PII"


@runtime_checkable
class PiiDetector(Protocol):
    """Protocol for PII detection backends."""

    def detect(self, text: str) -> list[PiiMatch]: ...


# ---------------------------------------------------------------------------
# Checksum validators (stdlib arithmetic, zero-dep) — ADR-424
#
# These gate CREDIT_CARD / IBAN / ABA_ROUTING matches: a regex hit only
# counts as PII if the checksum also passes, cutting false positives on
# order numbers / arbitrary IDs that happen to be the right shape.
# ---------------------------------------------------------------------------


def luhn_valid(digits: str) -> bool:
    """Luhn (mod-10) checksum for credit-card-shaped numbers.

    Doubles every second digit counting from the rightmost digit; digits
    that double past 9 have 9 subtracted. Valid iff the total is a
    multiple of 10. Strips non-digit separators (spaces/dashes) first.
    """
    cleaned = re.sub(r"\D", "", digits)
    if not cleaned:
        return False
    total = 0
    for i, ch in enumerate(reversed(cleaned)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_mod97_valid(value: str) -> bool:
    """IBAN mod-97 checksum.

    Move the first four characters to the end, map letters to numbers
    (A=10 .. Z=35), then the resulting big integer mod 97 must equal 1.
    """
    cleaned = re.sub(r"\s+", "", value).upper()
    if len(cleaned) < 4:
        return False
    rearranged = cleaned[4:] + cleaned[:4]
    try:
        # int(ch, 36) maps '0'-'9' -> 0-9 and 'A'-'Z' -> 10-35, exactly
        # the IBAN letter-to-number substitution.
        numeric_str = "".join(str(int(ch, 36)) for ch in rearranged)
        return int(numeric_str) % 97 == 1
    except ValueError:
        return False


def aba_checksum_valid(digits: str) -> bool:
    """ABA/MICR routing-number checksum: weighted 3-7-1 pattern, mod 10 == 0."""
    cleaned = re.sub(r"\D", "", digits)
    if len(cleaned) != 9:
        return False
    d = [int(c) for c in cleaned]
    checksum = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    return checksum % 10 == 0


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PatternEntry:
    """One detectable entity: its regex, optional checksum gate, and toggle category.

    ``category`` is the name the ``pii_entities`` allow/deny toggle
    operates on. It usually equals ``pii_type`` (e.g. "SSN"), except the
    SECRETS group where every specific secret type (AWS_ACCESS_KEY, JWT,
    ...) shares one togglable category — ADR-423, one detect/redact path.
    """

    pii_type: str
    category: str
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None
    namespace: str = "PII"


# Gov/CUI entities with NO checksum are a real false-positive source (a
# bare 10-digit EDIPI also matches phone numbers / order IDs; US
# driver's-license and passport formats vary too widely for one shape).
# Per SDD Research Insight #2 these ship DEFAULT-OFF — an operator must
# opt in via `pii_entities.allow` to scan for them. Each pattern also
# requires a proximate keyword anchor (EDIPI/DoD ID/passport/DL/MRN) to
# keep even the opt-in path reasonably precise (ADR-425/426).
DEFAULT_OFF_ENTITIES: frozenset[str] = frozenset(
    {"US_PASSPORT", "US_DRIVERS_LICENSE", "DOD_ID", "CAC", "BANK_ACCOUNT", "DOB", "MRN"}
)

_BUILTIN_PATTERNS: list[_PatternEntry] = [
    _PatternEntry("SSN", "SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    _PatternEntry(
        "CREDIT_CARD",
        "CREDIT_CARD",
        re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
        validator=luhn_valid,
    ),
    _PatternEntry(
        "EMAIL", "EMAIL", re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
    ),
    # Requires separators, prefix, or parenthesized area code to reduce
    # false positives on serial numbers and numeric identifiers.
    _PatternEntry(
        "PHONE",
        "PHONE",
        re.compile(
            r"(?:"
            r"\b(?:\+?1[-.\s]?)\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"  # +1/1 prefixed
            r"|"
            r"\(\d{3}\)[-.\s]?\d{3}[-.\s]?\d{4}\b"  # (xxx) xxx-xxxx
            r"|"
            r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"  # xxx-xxx-xxxx (separated)
            r")"
        ),
    ),
    _PatternEntry("IPV4", "IPV4", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    _PatternEntry(
        "IPV6",
        "IPV6",
        re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b"),
    ),
    _PatternEntry(
        "IBAN",
        "IBAN",
        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
        validator=iban_mod97_valid,
    ),
    _PatternEntry(
        "ABA_ROUTING",
        "ABA_ROUTING",
        re.compile(r"\b\d{9}\b"),
        validator=aba_checksum_valid,
    ),
    _PatternEntry(
        "US_PASSPORT",
        "US_PASSPORT",
        re.compile(r"(?i)\bpassport\s*(?:no\.?|number|#)?\s*:?\s*[A-Z]?\d{6,9}\b"),
    ),
    _PatternEntry(
        "US_DRIVERS_LICENSE",
        "US_DRIVERS_LICENSE",
        re.compile(
            r"(?i)\b(?:driver'?s?\s*license|DL)\s*(?:no\.?|number|#)?\s*:?\s*[A-Z0-9]{5,12}\b"
        ),
    ),
    _PatternEntry(
        "DOD_ID",
        "DOD_ID",
        re.compile(r"(?i)\b(?:EDIPI|DoD\s*ID)\s*(?:no\.?|number|#)?\s*:?\s*\d{10}\b"),
    ),
    _PatternEntry(
        "CAC",
        "CAC",
        re.compile(r"(?i)\bCAC\s*(?:no\.?|number|#)?\s*:?\s*\d{10}\b"),
    ),
    _PatternEntry(
        "BANK_ACCOUNT",
        "BANK_ACCOUNT",
        re.compile(r"(?i)\b(?:bank\s*account|acct)\s*(?:no\.?|number|#)?\s*:?\s*\d{6,17}\b"),
    ),
    _PatternEntry(
        "DOB",
        "DOB",
        re.compile(r"(?i)\b(?:DOB|date\s*of\s*birth)\s*:?\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b"),
    ),
    _PatternEntry(
        "MRN",
        "MRN",
        re.compile(r"(?i)\bMRN\s*(?:no\.?|number)?\s*:?\s*[A-Z0-9]{6,12}\b"),
    ),
    *(
        _PatternEntry(secret_type, SECRETS_CATEGORY, pattern, namespace="SECRET")
        for secret_type, pattern in SECRET_PATTERNS
    ),
]

# All built-in toggle-category names (custom_patterns extend this per-instance).
ALL_CATEGORIES: frozenset[str] = frozenset(entry.category for entry in _BUILTIN_PATTERNS)


# ---------------------------------------------------------------------------
# pii_entities allow/deny resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityToggle:
    """Resolved allow/deny decision for the ``pii_entities`` config.

    Resolution order (allow wins if both are given — explicit intent):
        allow given  -> only those categories scan
        deny given   -> all default-enabled categories minus denied
        neither      -> all default-enabled categories
            (DEFAULT_OFF_ENTITIES excluded unless explicitly allow-listed)
    """

    enabled: frozenset[str]

    @classmethod
    def from_config(
        cls,
        config: dict[str, list[str]] | None,
        known_categories: frozenset[str] = ALL_CATEGORIES,
        default_off: frozenset[str] = DEFAULT_OFF_ENTITIES,
    ) -> EntityToggle:
        """Build an EntityToggle from a ``pii_entities`` config dict.

        Raises:
            ArcLLMConfigError: If an allow/deny entry names an unknown
                category (typo fails loud at construction).
        """
        cfg = config or {}
        allow = cfg.get("allow")
        deny = cfg.get("deny", [])

        if allow is not None:
            allow_set = frozenset(allow)
            unknown = allow_set - known_categories
            if unknown:
                raise ArcLLMConfigError(f"Unknown pii_entities categories: {sorted(unknown)}")
            return cls(enabled=allow_set)

        deny_set = frozenset(deny)
        unknown = deny_set - known_categories
        if unknown:
            raise ArcLLMConfigError(f"Unknown pii_entities categories: {sorted(unknown)}")
        default_enabled = known_categories - default_off
        return cls(enabled=default_enabled - deny_set)


class RegexPiiDetector:
    """PII detector using compiled regex patterns.

    Ships with built-in patterns for SSN, credit card, email, phone, IPv4,
    IPv6, IBAN, ABA routing, gov/CUI entities (passport, driver's license,
    DoD ID/EDIPI, CAC, bank account, DOB, MRN — default-off), and a
    togglable SECRETS category. Accepts additional custom patterns and a
    ``pii_entities`` allow/deny toggle via the constructor.
    """

    def __init__(
        self,
        custom_patterns: list[dict[str, str]] | None = None,
        entities: dict[str, list[str]] | None = None,
    ) -> None:
        self._patterns: list[_PatternEntry] = list(_BUILTIN_PATTERNS)
        if custom_patterns:
            for entry in custom_patterns:
                name = entry["name"]
                try:
                    compiled = re.compile(entry["pattern"])
                except re.error as e:
                    raise ArcLLMConfigError(
                        f"Invalid regex for custom PII pattern '{name}': {e}"
                    ) from e
                self._patterns.append(_PatternEntry(name, name, compiled))

        known_categories = ALL_CATEGORIES | {p.category for p in self._patterns}
        self._toggle = EntityToggle.from_config(entities, known_categories=known_categories)

    def detect(self, text: str) -> list[PiiMatch]:
        """Scan text for PII patterns.

        Returns non-overlapping matches sorted by start position.
        When matches overlap, the longer match takes priority. A
        candidate match only counts if (a) its category is enabled by
        the entity toggle and (b) its checksum validator (if any) passes.

        Only the first ``MAX_REGEX_SCAN_LENGTH`` characters are scanned
        (LLM10) — a single huge message must not run every pattern in
        this registry over an unbounded input synchronously on the event
        loop. PII placed past the cap is a documented, deliberate gap.
        """
        if not text:
            return []
        text = text[:MAX_REGEX_SCAN_LENGTH]

        all_matches = [
            PiiMatch(
                pii_type=entry.pii_type,
                start=m.start(),
                end=m.end(),
                matched_text=m.group(),
                namespace=entry.namespace,
            )
            for entry in self._patterns
            if entry.category in self._toggle.enabled
            for m in entry.pattern.finditer(text)
            if entry.validator is None or entry.validator(m.group())
        ]

        if not all_matches:
            return []

        # Sort by start position, then by length descending (longer wins)
        all_matches.sort(key=lambda m: (m.start, -(m.end - m.start)))

        # Remove overlapping matches (keep first = longest at each position)
        filtered: list[PiiMatch] = []
        last_end = -1
        for match in all_matches:
            if match.start >= last_end:
                filtered.append(match)
                last_end = match.end

        return filtered


def redact_text(text: str, matches: list[PiiMatch]) -> str:
    """Replace PII/secret matches with ``[NAMESPACE:TYPE]`` placeholders.

    Processes matches in reverse order to preserve string indices.
    """
    if not matches:
        return text

    # Process in reverse order so earlier replacements don't shift indices
    result = text
    for match in reversed(matches):
        tag = f"[{match.namespace}:{match.pii_type}]"
        result = result[: match.start] + tag + result[match.end :]
    return result
