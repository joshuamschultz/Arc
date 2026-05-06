"""End-to-end messaging round-trip on the live cloud deploy.

Drives the Messages page in a real browser:
  1. Pick agent (default nlit_soc_agent).
  2. Wait for the chat WS `ready` frame (composer enables when received).
  3. Type a unique message; submit.
  4. Wait for a real agent reply (the placeholder "…thinking" doesn't count).
  5. Print verdict + screenshot.

Usage:
    .venv/bin/python scripts/playwright_messaging_test.py --token <viewer>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://agent.blackarcsystems.com")
    p.add_argument("--token", required=True)
    p.add_argument("--agent", default="nlit_soc_agent")
    p.add_argument("--out", default="/tmp/arc-audit/messages-roundtrip.png")
    args = p.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    marker = f"PINGCHK-{int(time.time())}"
    message = f"Reply with one short sentence containing the token {marker}."

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 950}, ignore_https_errors=True)
        ctx.add_init_script(
            f"window.localStorage.setItem('arcui_viewer_token', {json.dumps(args.token)});"
        )
        page = ctx.new_page()

        ws_frames: list[str] = []

        def _on_ws(ws: object) -> None:
            ws.on("framereceived", lambda d: ws_frames.append(("recv", d)))
            ws.on("framesent", lambda d: ws_frames.append(("sent", d)))

        page.on("websocket", _on_ws)
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(f"{args.url}/?page=messages#auth={args.token}", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # Pick the agent
        try:
            page.locator(f'[data-agent-id="{args.agent}"]').first.click(timeout=8000)
        except Exception as e:
            print(f"✗ could not click {args.agent}: {e}")
            page.screenshot(path=args.out, full_page=True)
            return 1

        # Wait for composer to enable — that's when the chat WS sent its `ready`.
        try:
            page.wait_for_selector("[data-msg-input]:not([disabled])", timeout=15000)
        except Exception:
            print("✗ composer never enabled (chat WS ready frame did not arrive)")
            page.screenshot(path=args.out, full_page=True)
            return 1

        page.fill("[data-msg-input]", message)
        page.locator("[data-msg-send]").first.click()
        print(f"sent → '{message}'")

        # Wait for an agent reply with our marker. The placeholder "…thinking"
        # is replaced in place once the real reply lands; we filter on marker.
        try:
            page.wait_for_function(
                f"""() => {{
                    const txt = document.body.innerText || '';
                    return txt.includes({json.dumps(marker)});
                }}""",
                timeout=180000,  # the agent talks to Anthropic — give it room
            )
            verdict = "✓"
        except Exception as e:
            verdict = "✗"
            print(f"timed out waiting for marker '{marker}' in body: {e}")

        page.screenshot(path=args.out, full_page=True)
        n_recv = sum(1 for f in ws_frames if f[0] == "recv")
        n_sent = sum(1 for f in ws_frames if f[0] == "sent")
        print(f"{verdict}  ws_sent={n_sent}  ws_recv={n_recv}")
        if page_errors:
            print(f"   page_errors: {page_errors[:3]}")
        print(f"   screenshot: {args.out}")
        browser.close()

    return 0 if verdict == "✓" else 1


if __name__ == "__main__":
    sys.exit(main())
