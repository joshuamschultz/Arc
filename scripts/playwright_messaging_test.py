"""End-to-end messaging round-trip on the live cloud deploy.

Drives the Messages page in a real browser:
  1. Pick agent (nlit_soc_agent).
  2. Type "Hi from Playwright" and submit.
  3. Wait for an agent response message bubble to render.
  4. Print verdict + screenshot.

Run:
    .venv/bin/python scripts/playwright_messaging_test.py --token <viewer>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://agent.blackarcsystems.com")
    p.add_argument("--token", required=True)
    p.add_argument("--agent", default="nlit_soc_agent")
    p.add_argument("--message", default="Hi from Playwright. Reply with one short sentence.")
    p.add_argument("--out", default="/tmp/arc-audit/messages-roundtrip.png")
    args = p.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 950}, ignore_https_errors=True)
        ctx.add_init_script(
            f"window.localStorage.setItem('arcui_viewer_token', {json.dumps(args.token)});"
        )
        page = ctx.new_page()

        api_calls: list[dict[str, object]] = []
        page.on(
            "response",
            lambda r: api_calls.append({"status": r.status, "url": r.url})
            if "/api/" in r.url or "/ws" in r.url
            else None,
        )
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(f"{args.url}/?page=messages#auth={args.token}", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # Pick agent in the messages-page channel list (data-agent-id is its selector)
        agent_card = page.locator(f'[data-agent-id="{args.agent}"]').first
        try:
            agent_card.click(timeout=8000)
        except Exception as e:
            print(f"✗ couldn't click agent card: {e}")
            print(f"  visible cards: {page.locator('[data-agent]').count()}")
            page.screenshot(path=args.out, full_page=True)
            return 1
        page.wait_for_timeout(500)

        # The composer is `[data-msg-input]` and `[data-msg-send]` per messages-page.js.
        try:
            page.wait_for_selector("[data-msg-input]:not([disabled])", timeout=5000)
            page.fill("[data-msg-input]", args.message)
            send_btn = page.locator("[data-msg-send]").first
            if send_btn.count():
                send_btn.click()
            else:
                page.keyboard.press("Enter")
        except Exception as e:
            print(f"✗ couldn't operate composer: {e}")
            page.screenshot(path=args.out, full_page=True)
            return 1

        # Wait for the user bubble to render, then for an agent reply.
        print(f"sent → '{args.message}'")
        # Capture initial bubble count, wait for it to grow by >=2 (user + agent).
        initial = page.locator(".message-bubble, .msg, [class*=message]").count()
        try:
            page.wait_for_function(
                f"() => document.querySelectorAll('.message-bubble, .msg, [class*=message]').length >= {initial + 2}",
                timeout=120000,
            )
            verdict = "✓"
        except Exception as e:
            verdict = "✗"
            print(f"timeout waiting for round-trip: {e}")
            print(f"   initial bubbles: {initial}, current: {page.locator('.message-bubble, .msg, [class*=message]').count()}")

        bubbles = page.locator(".message-bubble, .msg, [class*=message]")
        n = bubbles.count()
        page.screenshot(path=args.out, full_page=True)

        ws_calls = [c for c in api_calls if "/ws" in str(c["url"])]
        api_failures = [c for c in api_calls if isinstance(c["status"], int) and c["status"] >= 400]

        print(f"{verdict}  bubbles={n}  ws_calls={len(ws_calls)}  api_failures={len(api_failures)}")
        for f in api_failures[:5]:
            print(f"   API {f['status']}  {f['url']}")
        if page_errors:
            print(f"   page_errors: {page_errors[:3]}")
        print(f"   screenshot: {args.out}")
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)

        browser.close()
    return 0 if verdict == "✓" else 1


if __name__ == "__main__":
    sys.exit(main())
