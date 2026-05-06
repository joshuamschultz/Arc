#!/usr/bin/env -S python3 -u
"""Synthesize a "T-30 days" drift artifact for Act 4 of the NLIT demo.

Reads the sanitized workstation XCCDF (current scan), flips a curated
set of currently-failing rules back to pass, and rolls timestamps back
30 days. The diff between the produced T-30 file and the current
sanitized file becomes the demonstrable regression in Act 4.

Per SPEC-024 D-371: programmatic generator (over hand-edit) for
reproducibility. Same input + same flip-list → byte-identical output.

Categorized regressions (currently failing → flip to pass in T-30):

  SSH hardening cluster      AC-17, IA-2, CM-7  →  MITRE T1110.001
  Account lockout regression AC-7              →  brute-force narrative
  Audit chain weakening      AU-2, AU-12       →  log-tamper narrative
  File-integrity package     CM-6, SI-7        →  removed aide
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lxml import etree

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "demo-data" / "sanitized" / "linux-ws-01.demo.local.xml"
DST = REPO_ROOT / "demo-data" / "sanitized" / "linux-ws-01.t-30.xml"

NS = {"x": "http://checklists.nist.gov/xccdf/1.2"}

# Rule IDs (without the xccdf_org.ssgproject.content_rule_ prefix)
# that currently FAIL and that we'll flip to PASS in the T-30 fork.
# Hand-picked across categories for narrative breadth in Act 4.
DRIFT_RULES_SHORT: list[str] = [
    # SSH hardening cluster — AC-17 remote access, IA-2 auth, CM-7 least-functionality
    "sshd_disable_empty_passwords",       # HIGH severity
    "sshd_disable_root_login",            # AC-6(2), AC-17, IA-2
    "sshd_set_idle_timeout",              # AC-17, AC-2(5)
    "sshd_set_keepalive",                 # AC-2(5), AC-12, AC-17
    "sshd_disable_gssapi_auth",           # CM-7
    # Account lockout regression — AC-7 brute force prevention
    "accounts_passwords_pam_faillock_deny",
    "accounts_passwords_pam_faillock_audit",
    # Audit chain weakening — AU-2, AU-12 audit integrity
    "audit_rules_immutable",
    "audit_rules_file_deletion_events_unlink",
    # File-integrity package removed — CM-6, SI-7
    "package_aide_installed",
]

DRIFT_RULES = [
    f"xccdf_org.ssgproject.content_rule_{short}" for short in DRIFT_RULES_SHORT
]

DAYS_BACK = 30


def shift_iso_timestamp(ts: str, days: int) -> str:
    """Subtract `days` from an ISO-8601 timestamp, preserving tz info."""
    # Python 3.11+ accepts "+00:00" tz directly via fromisoformat.
    dt = datetime.fromisoformat(ts)
    return (dt - timedelta(days=days)).isoformat()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=str(SRC), help=f"source XCCDF (default: {SRC.name})")
    ap.add_argument("--dst", default=str(DST), help=f"output T-30 XCCDF (default: {DST.name})")
    ap.add_argument("--days-back", type=int, default=DAYS_BACK)
    args = ap.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.dst)
    if not src.exists():
        print(f"Error: source not found: {src}", file=sys.stderr)
        return 2

    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(src), parser)
    root = tree.getroot()

    # 1. Shift TestResult start/end timestamps
    test_result = root.find(".//x:TestResult", NS)
    if test_result is None:
        print("Error: no TestResult element in source", file=sys.stderr)
        return 2

    for attr in ("start-time", "end-time"):
        v = test_result.get(attr)
        if v:
            test_result.set(attr, shift_iso_timestamp(v, args.days_back))

    # 2. Find rule-result elements matching DRIFT_RULES, flip result, shift time
    target_set = set(DRIFT_RULES)
    flipped = 0
    not_found: list[str] = []
    for rule_id in DRIFT_RULES:
        rr = test_result.find(f"x:rule-result[@idref='{rule_id}']", NS)
        if rr is None:
            not_found.append(rule_id)
            continue
        # Shift this rule-result's time
        t = rr.get("time")
        if t:
            rr.set("time", shift_iso_timestamp(t, args.days_back))
        # Flip <result>fail</result> → <result>pass</result>
        result_el = rr.find("x:result", NS)
        if result_el is None:
            print(f"Warning: no <result> element in rule-result for {rule_id}", file=sys.stderr)
            continue
        old = (result_el.text or "").strip().lower()
        result_el.text = "pass"
        flipped += 1
        print(f"  flipped: {rule_id.replace('xccdf_org.ssgproject.content_rule_','')}  ({old} -> pass)")

    if not_found:
        print(f"\nWarning: {len(not_found)} target rules not found in source:", file=sys.stderr)
        for r in not_found:
            print(f"  {r}", file=sys.stderr)

    # 3. Shift every other rule-result's time too (so the whole scan looks
    # like it ran 30 days ago, not just the flipped rules)
    for rr in test_result.findall("x:rule-result", NS):
        if rr.get("idref") in target_set:
            continue  # already shifted
        t = rr.get("time")
        if t:
            rr.set("time", shift_iso_timestamp(t, args.days_back))

    # 4. Write output, deterministic XML serialization
    dst.parent.mkdir(parents=True, exist_ok=True)
    tree.write(
        str(dst),
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=False,
    )
    print(f"\nFlipped {flipped}/{len(DRIFT_RULES)} rules. Wrote: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
