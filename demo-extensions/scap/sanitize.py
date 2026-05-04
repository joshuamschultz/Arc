"""Deterministic sanitization for SCAP source data (SPEC-024 D-370, D-377).

Replaces real customer-system identifiers (hostnames, FQDNs, IPs, MACs)
with rebranded ``*.demo.local`` equivalents. Preserves rule IDs, CCIs,
800-53 mappings, finding text, and fix text verbatim — that's the
demo's credibility.

Operates by exact string substitution: the discovery step scans each
source file for known sensitive identifiers, builds a deterministic
mapping table, and applies it as text find-replace. No format-specific
parsers are needed for sanitization itself — XML/HTML/CSV all pass
through bytes-level substitution unchanged in structure.

The mapping is persisted to ``~/.arc/capabilities/scap/data/sanitize_map.toml``
on first run. Subsequent runs read the existing map; only new entries are
appended (idempotent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomli_w

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import]

from .models import Finding


# Per-file substitution targets. Reviewable, transparent, deterministic.
# Each entry: (source filename suffix → list of (original, sanitized) pairs).
# Unrecognized files are passed through unchanged.
HOST_ALIASES: dict[str, str] = {
    # Source filename → demo host alias.  Only the canonical-format file
    # per host is listed; companion exports (e.g. the workstation .csv
    # and .html duplicates of the same scan) are excluded so we don't
    # ship 13 MB of redundant unsanitized HTML.
    "stig-wkstn01.ipa.local.xml": "linux-ws-01.demo.local",
    "ELAM-SECRETSERVER_SCC-5.14_2026-04-22_081123_All-Settings_Windows_Server_2019_STIG-003.008.html": "win2019-app-01.demo.local",
    "ELAM-SECRETSERVER_SCC-5.14_2026-04-22_081123_Non-Compliance_Windows_Server_2019_STIG-003.008.html": "win2019-app-01.demo.local",
    "Palo-NDM-STIG.csv": "paloalto-fw-01.demo.local",
    "NXOS-NDM-STIG.csv": "cisco-nxos-01.demo.local",
}

# Output filename mapping (preserve format extension).
OUTPUT_FILENAMES: dict[str, str] = {
    "stig-wkstn01.ipa.local.xml": "linux-ws-01.demo.local.xml",
    "ELAM-SECRETSERVER_SCC-5.14_2026-04-22_081123_All-Settings_Windows_Server_2019_STIG-003.008.html": "win2019-app-01.demo.local.all-settings.html",
    "ELAM-SECRETSERVER_SCC-5.14_2026-04-22_081123_Non-Compliance_Windows_Server_2019_STIG-003.008.html": "win2019-app-01.demo.local.non-compliance.html",
    "Palo-NDM-STIG.csv": "paloalto-fw-01.demo.local.csv",
    "NXOS-NDM-STIG.csv": "cisco-nxos-01.demo.local.csv",
}

# Explicit substitution targets per source filename. Real customer
# system identifiers found by inspecting each file. Anything not listed
# here passes through unchanged — example IPs in rule prose
# (1.1.1.1, 10.1.x.x sample switch configs, etc.) are not customer
# infrastructure and don't need scrubbing.
SUBSTITUTIONS: dict[str, list[tuple[str, str]]] = {
    "stig-wkstn01.ipa.local.xml": [
        ("wkstn01.ipa.local", "linux-ws-01.demo.local"),
        ("wkstn01", "linux-ws-01"),
        ("192.168.55.101", "10.42.55.101"),
        ("192.168.122.1", "10.42.122.1"),
        ("fe80:0:0:0:250:56ff:fe90:2486", "fe80::42:42:42:42"),
        ("00:50:56:90:24:86", "02:00:00:55:24:86"),
        ("52:54:00:03:38:CE", "02:00:00:55:38:ce"),
    ],
    "ELAM-SECRETSERVER_SCC-5.14_2026-04-22_081123_All-Settings_Windows_Server_2019_STIG-003.008.html": [
        ("ELAM-SECRETSERVER.elam.local", "win2019-app-01.demo.local"),
        ("ELAM-SECRETSERVER", "win2019-app-01"),
        ("elam.local", "demo.local"),
        ("192.168.55.122", "10.42.55.122"),
        ("00:0C:29:AF:E1:F7", "02:00:00:55:e1:f7"),
    ],
    "ELAM-SECRETSERVER_SCC-5.14_2026-04-22_081123_Non-Compliance_Windows_Server_2019_STIG-003.008.html": [
        ("ELAM-SECRETSERVER.elam.local", "win2019-app-01.demo.local"),
        ("ELAM-SECRETSERVER", "win2019-app-01"),
        ("elam.local", "demo.local"),
        ("192.168.55.122", "10.42.55.122"),
        ("00:0C:29:AF:E1:F7", "02:00:00:55:e1:f7"),
    ],
    # Network device CSVs have no real customer infrastructure in their
    # content — FQDN/IP/MAC/Host Name columns are blank, only example
    # configs in rule prose. No substitutions needed.
    "Palo-NDM-STIG.csv": [],
    "NXOS-NDM-STIG.csv": [],
}


def host_alias_for(src_filename: str) -> str:
    """Look up the demo host alias for a source filename."""
    return HOST_ALIASES.get(src_filename, src_filename)


def output_filename_for(src_filename: str) -> str:
    """Look up the sanitized output filename."""
    return OUTPUT_FILENAMES.get(src_filename, src_filename)


def substitutions_for(src_filename: str) -> list[tuple[str, str]]:
    """Look up substitution pairs for a source filename."""
    return list(SUBSTITUTIONS.get(src_filename, []))


def apply_to_text(text: str, subs: list[tuple[str, str]]) -> str:
    """Apply each (original, sanitized) substitution to the text body.

    Substitutions run in the listed order; longest originals first when
    there's overlap (e.g. ``wkstn01.ipa.local`` before ``wkstn01``) so
    short prefixes don't catch the longer string mid-replace.
    """
    ordered = sorted(subs, key=lambda kv: -len(kv[0]))
    out = text
    for original, replacement in ordered:
        if original:
            out = out.replace(original, replacement)
    return out


def sanitize_file(src: Path, dst: Path, subs: list[tuple[str, str]] | None = None) -> dict[str, int]:
    """Read src, apply substitutions, write to dst. Returns counts."""
    if subs is None:
        subs = substitutions_for(src.name)
    text = src.read_text(encoding="utf-8", errors="replace")
    counts: dict[str, int] = {}
    for original, _ in subs:
        if original:
            counts[original] = text.count(original)
    sanitized = apply_to_text(text, subs)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(sanitized, encoding="utf-8")
    return counts


def apply_to_findings(findings: list[Finding], host_alias: str) -> list[Finding]:
    """Stamp the demo host alias onto every finding (no other rewriting needed
    once the source file was sanitized at write-time).
    """
    return [
        f.model_copy(update={"host_alias": host_alias}) for f in findings
    ]


def write_map_toml(map_path: Path, source_to_subs: dict[str, list[tuple[str, str]]]) -> None:
    """Persist the sanitization mapping to TOML for auditor review (D-370)."""
    payload: dict[str, Any] = {
        "_about": {
            "purpose": "Deterministic source-data sanitization for the NLIT SCAP demo (SPEC-024).",
            "what_changes": "Customer-system identifiers (hostnames, FQDNs, IPs, MACs) only.",
            "what_does_not_change": "Rule IDs, CCIs, NIST 800-53 mappings, finding text, fix text, severity, status, scanner version, timestamps.",
            "review": "Every entry below is a literal find-replace applied to the source file before commit.",
        },
        "host_aliases": HOST_ALIASES,
        "output_filenames": OUTPUT_FILENAMES,
        "substitutions": {
            src: [{"from": o, "to": s} for o, s in subs]
            for src, subs in source_to_subs.items()
        },
    }
    map_path.parent.mkdir(parents=True, exist_ok=True)
    with map_path.open("wb") as f:
        tomli_w.dump(payload, f)


def read_map_toml(map_path: Path) -> dict[str, Any]:
    if not map_path.exists():
        return {}
    with map_path.open("rb") as f:
        return tomllib.load(f)
