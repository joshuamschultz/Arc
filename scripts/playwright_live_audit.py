"""Drive the live cloud deploy and audit every page.

Records, for each page in the SPA:
  - HTTP status of the supporting API calls (from network capture)
  - Console errors / page errors
  - Whether the rendered DOM has expected elements

Run:
    .venv/bin/python scripts/playwright_live_audit.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


PAGES = [
    ("agents", "Agent Fleet", ".agent-card", 2),
    ("agent-detail&agent=nlit_soc_agent", "Detail (nlit_soc_agent)", ".ad-tabs", 1),
    ("messages", "Messages", '[data-page-content="messages"]:not(.hidden)', None),
    ("knowledge&agent=nlit_soc_agent", "Knowledge (nlit_soc_agent)", '[data-knowledge-context]', None),
    ("telemetry", "ArcLLM (telemetry/cost)", '[data-page-content="telemetry"]:not(.hidden)', None),
    ("tasks", "Tasks", '[data-page-content="tasks"]:not(.hidden)', None),
    ("tools-skills", "Tools / Skills", '[data-page-content="tools-skills"]:not(.hidden)', None),
    ("security", "Security", '[data-page-content="security"]:not(.hidden)', None),
    ("policy", "Policy", '[data-page-content="policy"]:not(.hidden)', None),
    ("settings", "Settings", '[data-page-content="settings"]:not(.hidden)', None),
]


def audit(url: str, token: str, screenshot_dir: Path) -> dict[str, Any]:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 950}, ignore_https_errors=True)
        # Inject viewer token into localStorage so the page bypasses the
        # auth-strip in the bootstrap and acts like a returning user.
        ctx.add_init_script(
            f"window.localStorage.setItem('arcui_viewer_token', {json.dumps(token)});"
        )

        for page_id, label, selector, expected_count in PAGES:
            page = ctx.new_page()
            api_results: list[dict[str, Any]] = []
            console_errors: list[str] = []
            page_errors: list[str] = []

            def _on_response(resp: Any, _api=api_results) -> None:
                req_url = resp.url
                if "/api/" in req_url:
                    _api.append({"status": resp.status, "url": req_url})

            def _on_console(msg: Any, _err=console_errors) -> None:
                if msg.type in ("error", "warning"):
                    _err.append(f"[{msg.type}] {msg.text}")

            page.on("response", _on_response)
            page.on("pageerror", lambda e, _err=page_errors: _err.append(str(e)))
            page.on("console", _on_console)

            target = f"{url}/?page={page_id}#auth={token}"
            page.goto(target, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2500)  # let async API calls settle

            try:
                page.wait_for_selector(selector, timeout=8000)
                rendered_count = page.locator(selector).count()
                rendered = True
            except Exception as e:
                rendered_count = 0
                rendered = False
                page_errors.append(f"selector_timeout: {selector} ({e})")

            screenshot_path = screenshot_dir / f"{page_id.split('&')[0]}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)

            api_failures = [r for r in api_results if r["status"] >= 400]

            ok = (
                rendered
                and not page_errors
                and not api_failures
                and (expected_count is None or rendered_count >= expected_count)
            )
            results[label] = {
                "ok": ok,
                "page_id": page_id,
                "rendered": rendered,
                "rendered_count": rendered_count,
                "expected_count": expected_count,
                "screenshot": str(screenshot_path),
                "api_calls": len(api_results),
                "api_failures": api_failures,
                "console_errors": console_errors[:5],
                "page_errors": page_errors[:5],
            }
            page.close()

        browser.close()
    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://agent.blackarcsystems.com")
    p.add_argument("--token", required=True, help="viewer token")
    p.add_argument("--out", default="/tmp/arc-audit", help="screenshot dir")
    args = p.parse_args()

    out = Path(args.out)
    results = audit(args.url, args.token, out)

    print(f"\n{'='*70}\nLIVE AUDIT — {args.url}\n{'='*70}\n")
    counts: defaultdict[str, int] = defaultdict(int)
    for label, r in results.items():
        flag = "✓" if r["ok"] else "✗"
        counts["ok" if r["ok"] else "fail"] += 1
        rc = r["rendered_count"]
        ec = r["expected_count"]
        cnt_str = f" (rendered={rc}{f'/{ec}' if ec else ''})"
        print(f"  {flag}  {label:<30}{cnt_str}")
        if not r["ok"]:
            if r["page_errors"]:
                print(f"      page_errors: {r['page_errors'][:3]}")
            if r["api_failures"]:
                for f in r["api_failures"][:3]:
                    print(f"      API {f['status']}  {f['url']}")
            if r["console_errors"]:
                print(f"      console: {r['console_errors'][:2]}")
        print(f"      screenshot: {r['screenshot']}")

    print(f"\n{'='*70}")
    print(f"  PASSED: {counts['ok']}    FAILED: {counts['fail']}")
    print(f"{'='*70}\n")
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
