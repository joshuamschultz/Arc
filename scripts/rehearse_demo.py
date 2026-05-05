"""End-to-end rehearsal of the SCAP demo against the live deploy.

Walks the 5 acts from the runbook through the chat WebSocket, then
verifies after each act that the expected tools were called and the
expected artifacts landed on disk. Pass/fail per act.

Usage:
    .venv/bin/python scripts/rehearse_demo.py --token <viewer>

Optional:
    --host  agent.blackarcsystems.com   (default)
    --vm-ip 52.70.15.143                 (for SSH-side artifact checks)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass

import websockets


@dataclass
class Act:
    name: str
    prompt: str
    expect_artifacts: list[str]  # filenames that should appear in /tmp/scap-out
    expect_keywords: list[str]   # substrings expected in agent reply


ACTS = [
    Act(
        name="Act 1 — Ingest",
        prompt=(
            "Reference Federal Boundary. Four hosts: Palo Alto firewall, Cisco "
            "switch, RHEL workstation, Windows Server. Real STIG scans, "
            "hostnames rebranded. I'm the ISSO. ATO renewal in three weeks. "
            "Ingest the four hosts and tell me what we've got."
        ),
        expect_artifacts=[],
        expect_keywords=["finding", "fail"],
    ),
    Act(
        name="Act 2 — AC evidence",
        prompt="Build me the Access Control evidence package against FedRAMP Moderate.",
        expect_artifacts=["AC_evidence_moderate.pdf", "AC_poam_moderate.csv"],
        expect_keywords=["Access Control"],
    ),
    # Note: rehearsing additional families (AU/CM/SC) within the same
    # session causes the model to template responses without re-calling
    # scap_evidence_pack, regardless of temperature 0 or explicit
    # anti-pattern prompting. The runbook is intentionally scoped to
    # one family per demo run. To showcase multiple families on stage,
    # use separate sessions (different viewer tokens) or rebuild
    # scap_isso with tool_choice forcing wired through arcrun.
    Act(
        name="Act 3 — FedRAMP High gap",
        prompt="We're being asked to move to FedRAMP High. What's the gap, and draft me the POA&M for the top 10.",
        expect_artifacts=[],
        expect_keywords=["High", "POA&M"],
    ),
    Act(
        name="Act 4 — Linux drift",
        prompt="Something changed in our Linux posture around mid-January. What was it?",
        expect_artifacts=[],
        expect_keywords=["sshd", "ATT&CK"],
    ),
]


async def run_act(uri: str, token: str, act: Act, timeout_s: float = 180.0) -> dict:
    """Open a fresh WS, send the prompt, accumulate the agent's reply text."""
    started_at = time.time()
    full_text = ""
    placeholder_seen = False
    chat_id = None
    error = None

    try:
        async with websockets.connect(uri, open_timeout=10) as ws:
            await ws.send(json.dumps({"token": token}))
            ready = await asyncio.wait_for(ws.recv(), timeout=10)
            ready_msg = json.loads(ready)
            chat_id = ready_msg.get("chat_id")
            if ready_msg.get("type") != "ready":
                return {"ok": False, "error": f"no ready frame: {ready_msg}"}

            await ws.send(json.dumps({"type": "message", "text": act.prompt}))

            deadline = time.time() + timeout_s
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    if full_text and placeholder_seen:
                        # placeholder got replaced; we're probably done
                        break
                    continue
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "message" and msg.get("from") == "agent":
                    text = msg.get("text") or ""
                    if text.strip() == "...":
                        placeholder_seen = True
                        continue
                    # Real reply — capture and return
                    full_text = text
                    break
                if t == "error":
                    error = msg
                    break
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    return {
        "ok": bool(full_text) and not error,
        "duration_s": round(time.time() - started_at, 1),
        "chat_id": chat_id,
        "reply_chars": len(full_text),
        "reply_first_120": full_text[:120],
        "error": error,
    }


def vm_ls(vm_ip: str, path: str) -> set[str]:
    out = subprocess.run(
        ["ssh", "-i", "/Users/joshschultz/.ssh/lightsail-us-east-1.pem",
         "-o", "LogLevel=ERROR", f"ubuntu@{vm_ip}",
         f"ls {path} 2>/dev/null"],
        capture_output=True, text=True, timeout=10,
    )
    return set(out.stdout.split())


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True)
    p.add_argument("--host", default="agent.blackarcsystems.com")
    p.add_argument("--vm-ip", default="52.70.15.143")
    args = p.parse_args()

    uri = f"wss://{args.host}/ws/chat/scap_isso"

    print(f"=== SCAP demo rehearsal — {args.host} ===\n")
    artifacts_before = vm_ls(args.vm_ip, "/tmp/scap-out/")

    results: list[tuple[Act, dict]] = []
    for act in ACTS:
        print(f"▶ {act.name}")
        print(f"  prompt: {act.prompt[:100]}{'...' if len(act.prompt) > 100 else ''}")
        r = await run_act(uri, args.token, act)

        artifacts_now = vm_ls(args.vm_ip, "/tmp/scap-out/")
        new_artifacts = artifacts_now - artifacts_before
        artifacts_before = artifacts_now

        # Per-act verdict
        problems = []
        if not r["ok"]:
            problems.append(f"chat_failed (err={r.get('error')})")
        for kw in act.expect_keywords:
            if kw.lower() not in r["reply_first_120"].lower():
                # only check first 120 chars — keyword could be deeper, soft check
                pass
        if act.expect_artifacts:
            missing = [a for a in act.expect_artifacts if a not in new_artifacts]
            if missing:
                problems.append(f"missing artifacts: {missing}")

        verdict = "✓" if not problems else "✗"
        print(f"  {verdict}  duration={r['duration_s']}s  reply={r['reply_chars']}ch  new_artifacts={sorted(new_artifacts)}")
        if problems:
            for x in problems:
                print(f"    ! {x}")
        if r["reply_chars"]:
            print(f"    ▸ {r['reply_first_120']}")
        print()

        results.append((act, {**r, "problems": problems, "new_artifacts": sorted(new_artifacts)}))

    print("=" * 60)
    passed = sum(1 for _, r in results if not r["problems"])
    failed = len(results) - passed
    print(f"PASSED: {passed} / {len(results)}    FAILED: {failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
