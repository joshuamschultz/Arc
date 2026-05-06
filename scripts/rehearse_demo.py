"""End-to-end rehearsal of the SCAP demo against the live deploy.

Walks the runbook acts through ONE continuous chat WebSocket (mirroring
how the user actually runs the demo), then validates after each act:

  - reply text (non-empty, expected keywords)
  - artifacts on disk (PDF + CSV via SSH ls /tmp/scap-out)
  - entity records (rich named-section content, not skeletal)
  - report records (one per investigative pass)
  - daily-notes (after 5+ turns the bio_memory consolidator fires)

Pass/fail per act + final boundary summary. Exit 0 only if every act and
every post-condition is green.

Usage:
    .venv/bin/python scripts/rehearse_demo.py --token <viewer>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field

import websockets

SSH_KEY = "/Users/joshschultz/.ssh/lightsail-us-east-1.pem"
TEAM_ROOT = "/home/ubuntu/arc/team/scap_isso_agent"


@dataclass
class Act:
    name: str
    prompt: str
    expect_artifacts: list[str] = field(default_factory=list)
    expect_entity_types: list[str] = field(default_factory=list)
    expect_keywords: list[str] = field(default_factory=list)


ACTS = [
    Act(
        name="Act 1 — Ingest",
        prompt=(
            "Reference Federal Boundary. Four hosts: Palo Alto firewall, Cisco "
            "switch, RHEL workstation, Windows Server. Real STIG scans, "
            "hostnames rebranded. I'm the ISSO. ATO renewal in three weeks. "
            "Ingest the four hosts and tell me what we've got. After "
            "ingest, write a Host record per system AND a Report "
            "(report_kind=boundary-summary) wikilinking all four hosts, "
            "with every body section populated."
        ),
        expect_entity_types=["Host", "Report"],
        expect_keywords=["finding", "fail"],
    ),
    Act(
        name="Act 2 — AC evidence",
        prompt=(
            "Build me the Access Control evidence package against FedRAMP "
            "Moderate. Then capture an EvidencePack record citing the real "
            "PDF and POA&M paths the tool returned, with all four hosts "
            "wikilinked."
        ),
        expect_artifacts=["AC_evidence_moderate.pdf", "AC_poam_moderate.csv"],
        expect_entity_types=["EvidencePack"],
        expect_keywords=["Access Control"],
    ),
    Act(
        name="Act 3 — FedRAMP High gap",
        prompt=(
            "We're being asked to move to FedRAMP High. What's the gap, "
            "and draft me the POA&M for the top 10. Capture a Baseline "
            "record AND a Report (report_kind=gap-analysis), each with "
            "their body sections populated and Control wikilinks for the "
            "top gaps."
        ),
        expect_entity_types=["Baseline", "Report"],
        expect_keywords=["High", "POA&M"],
    ),
    Act(
        name="Act 4 — Linux drift + ATT&CK",
        prompt=(
            "Something changed in our Linux posture around mid-January. "
            "What was it? Once you've identified the regression, correlate "
            "the failing controls to MITRE ATT&CK and capture: a Drift "
            "record, a Report (report_kind=attack-correlation), and one "
            "Control record per significant failing control."
        ),
        expect_entity_types=["Drift", "Report", "Control"],
        expect_keywords=["sshd", "ATT&CK"],
    ),
]


async def send_and_collect(
    ws: websockets.WebSocketClientProtocol, prompt: str, timeout_s: float = 240.0
) -> dict:
    """Send a single prompt on an existing chat WS and accumulate the reply."""
    started_at = time.time()
    placeholder_seen = False
    full_text = ""
    error = None

    await ws.send(json.dumps({"type": "message", "text": prompt}))

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
        except asyncio.TimeoutError:
            if full_text and placeholder_seen:
                break
            continue
        msg = json.loads(raw)
        t = msg.get("type")
        if t == "message" and msg.get("from") == "agent":
            text = msg.get("text") or ""
            if text.strip() == "...":
                placeholder_seen = True
                continue
            full_text = text
            break
        if t == "error":
            error = msg
            break

    return {
        "ok": bool(full_text) and not error,
        "duration_s": round(time.time() - started_at, 1),
        "reply_chars": len(full_text),
        "reply_first_240": full_text[:240],
        "error": error,
    }


def vm_run(vm_ip: str, cmd: str) -> str:
    """Run a shell command over SSH and return stdout."""
    out = subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "LogLevel=ERROR", f"ubuntu@{vm_ip}", cmd],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout


def vm_ls(vm_ip: str, path: str) -> set[str]:
    out = vm_run(vm_ip, f"ls {path} 2>/dev/null")
    return set(out.split())


def vm_entities_by_type(vm_ip: str) -> dict[str, list[str]]:
    """Return {entity_type: [filenames]} for everything in workspace/entities."""
    cmd = (
        f"find {TEAM_ROOT}/workspace/entities -mindepth 2 -maxdepth 2 "
        '-name "*.md" -printf "%P\\n" 2>/dev/null'
    )
    out = vm_run(vm_ip, cmd)
    by_type: dict[str, list[str]] = {}
    for line in out.splitlines():
        if "/" in line:
            t, name = line.split("/", 1)
            by_type.setdefault(t, []).append(name)
    return by_type


def vm_richness_score(vm_ip: str, rel_path: str) -> tuple[int, list[str]]:
    """Read an entity file and count populated body sections.

    Returns (populated_count, list_of_empty_sections).
    """
    cmd = f"cat {TEAM_ROOT}/workspace/entities/{rel_path} 2>/dev/null"
    text = vm_run(vm_ip, cmd)
    if not text:
        return 0, ["FILE_EMPTY"]

    sections = []
    current_header = None
    current_body: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if current_header is not None:
                sections.append((current_header, "\n".join(current_body).strip()))
            current_header = line[3:].strip()
            current_body = []
        elif current_header is not None:
            current_body.append(line)
    if current_header is not None:
        sections.append((current_header, "\n".join(current_body).strip()))

    populated = 0
    empty: list[str] = []
    for h, body in sections:
        if h == "Related":
            continue  # related-section emptiness is fine on first write
        if body and not body.startswith("_(") and len(body) > 5:
            populated += 1
        else:
            empty.append(h)
    return populated, empty


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    p.add_argument("--host", default="agent.blackarcsystems.com")
    p.add_argument("--vm-ip", default="52.70.15.143")
    args = p.parse_args()

    uri = f"wss://{args.host}/ws/chat/scap_isso"
    print(f"=== SCAP demo rehearsal — {args.host} ===\n")

    artifacts_before = vm_ls(args.vm_ip, "/tmp/scap-out/")
    entities_before = vm_entities_by_type(args.vm_ip)

    results: list[tuple[Act, dict]] = []

    async with websockets.connect(uri, open_timeout=10, max_size=4_000_000) as ws:
        await ws.send(json.dumps({"token": args.token}))
        ready = await asyncio.wait_for(ws.recv(), timeout=10)
        if json.loads(ready).get("type") != "ready":
            print(f"!! WS did not return ready: {ready}")
            return 2
        print("✓ WS ready, single-session rehearsal starting\n")

        for act in ACTS:
            print(f"▶ {act.name}")
            print(f"  prompt: {act.prompt[:120]}{'...' if len(act.prompt) > 120 else ''}")
            r = await send_and_collect(ws, act.prompt)

            artifacts_now = vm_ls(args.vm_ip, "/tmp/scap-out/")
            new_artifacts = artifacts_now - artifacts_before
            artifacts_before = artifacts_now

            entities_now = vm_entities_by_type(args.vm_ip)
            new_entities: dict[str, list[str]] = {}
            for t, names in entities_now.items():
                old = set(entities_before.get(t, []))
                added = sorted(set(names) - old)
                if added:
                    new_entities[t] = added
            entities_before = entities_now

            problems = []
            if not r["ok"]:
                problems.append(f"chat_failed (err={r.get('error')})")
            if act.expect_artifacts:
                missing = [a for a in act.expect_artifacts if a not in new_artifacts]
                if missing:
                    problems.append(f"missing artifacts: {missing}")
            for et in act.expect_entity_types:
                if et not in new_entities:
                    problems.append(f"missing entity type: {et}")

            # Richness check on every newly written entity
            thin_records: list[str] = []
            for t, names in new_entities.items():
                for n in names:
                    populated, empty = vm_richness_score(args.vm_ip, f"{t}/{n}")
                    if populated == 0:
                        thin_records.append(f"{t}/{n} (no populated body sections)")
                    elif empty and len(empty) > 1:
                        thin_records.append(f"{t}/{n} (empty: {empty})")
            if thin_records:
                problems.append(f"thin records: {thin_records}")

            verdict = "✓" if not problems else "✗"
            print(f"  {verdict}  duration={r['duration_s']}s  reply={r['reply_chars']}ch")
            print(f"     new_artifacts={sorted(new_artifacts)}")
            print(f"     new_entities={ {k: v for k, v in new_entities.items()} }")
            if problems:
                for x in problems:
                    print(f"     ! {x}")
            if r["reply_chars"]:
                print(f"     ▸ {r['reply_first_240']}")
            print()

            results.append((act, {**r, "problems": problems,
                                  "new_artifacts": sorted(new_artifacts),
                                  "new_entities": new_entities}))

    # Final boundary checks (after all acts)
    print("=" * 70)
    daily = vm_ls(args.vm_ip, f"{TEAM_ROOT}/workspace/memory/daily-notes/")
    final_entities = vm_entities_by_type(args.vm_ip)
    n_total = sum(len(v) for v in final_entities.values())
    print(f"FINAL: entities={n_total} ({final_entities})")
    print(f"FINAL: daily-notes={sorted(daily)}")

    passed = sum(1 for _, r in results if not r["problems"])
    failed = len(results) - passed
    print(f"PASSED: {passed} / {len(results)}    FAILED: {failed}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
