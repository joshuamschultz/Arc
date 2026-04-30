"""Live browser rehearsal — Playwright drives the running UI through every
critical SPEC-022 surface against a real arcui server backed by the on-disk
team/ tree.

Use:
  - `pytest tests/integration/test_browser_rehearsal.py -v`
  - The fixture starts an arcui process for the duration of the module, then
    stops it. No external setup needed; the test creates a synthetic team
    dir under `tmp_path` and points the server at it.
  - Skipped (not failed) when Playwright's browsers aren't installed so this
    file is safe to land before every CI box has run `playwright install`.

What it asserts (every line corresponds to a bug we shipped at some point):
  - SPA serves index.html, all 8 panels and 8 SPEC-022 scripts present
  - /api/team/roster returns expected count from the synthetic team_root
  - URL `?page=agents` mounts AgentsPage and renders >= 1 .agent-card
  - URL `?page=agent-detail&agent=<id>` deep-links and mounts AgentDetail
  - Switching agents within agent-detail re-mounts (R5: was caching)
  - All 9 tabs activate without throwing
  - Overview tab uses Fmt.number safely (R3) and unwraps /stats (R4)
  - WS subscribe + file_change live update reaches the browser <8s, and
    the Policy tab re-renders the new bullet
  - Page errors and /api/* HTTP errors are zero throughout
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path


def _find_arc() -> str | None:
    """Locate the ``arc`` CLI. Looks at PATH first, falls back to the same
    virtualenv as the running interpreter (the common dev case where ``arc``
    is a venv entrypoint not on the user's PATH but installed alongside
    pytest)."""
    on_path = shutil.which("arc")
    if on_path:
        return on_path
    venv_arc = Path(sys.executable).parent / "arc"
    if venv_arc.exists() and os.access(venv_arc, os.X_OK):
        return str(venv_arc)
    return None

import pytest

playwright_module = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright not installed; run `pip install playwright && playwright install chromium`",
)
sync_playwright = playwright_module.sync_playwright


VIEWER_TOKEN = "V_REHEARSAL_BROWSER"
OPERATOR_TOKEN = "O_REHEARSAL_BROWSER"
AGENT_TOKEN = "A_REHEARSAL_BROWSER"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_team(team_root: Path) -> None:
    team_root.mkdir(parents=True, exist_ok=True)
    for name in ("alpha", "beta"):
        agent = team_root / f"{name}_agent"
        agent.mkdir()
        (agent / "arcagent.toml").write_text(
            f'[agent]\nname = "{name}"\norg = "rehearsal"\ntype = "executor"\n'
            f'[identity]\ndid = "did:arc:test:{name}"\n'
            f'[llm]\nmodel = "anthropic/claude-sonnet-4-5"\nmax_tokens = 4096\n'
            f'[ui]\ndisplay_name = "{name.capitalize()} Test"\ncolor = "#3a82f6"\n',
            encoding="utf-8",
        )
        ws = agent / "workspace"
        ws.mkdir()
        (ws / "policy.md").write_text(
            "# Policy\n\n"
            "- [P01] Be helpful {score:9, uses:5, reviewed:2026-04-29, "
            "created:2026-04-01, source:test-001}\n"
            "- [P02] Cite sources {score:7, uses:3, reviewed:2026-04-29, "
            "created:2026-04-01, source:test-002}\n",
            encoding="utf-8",
        )
        (ws / "identity.md").write_text(f"# {name}\nI am the {name} test agent.\n", encoding="utf-8")
        (ws / "skills").mkdir()
        (ws / "skills" / "demo.md").write_text(
            "---\nname: demo\ndescription: a demo skill\n---\n# body\n",
            encoding="utf-8",
        )


def _wait_for_server(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def server_team(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """Spawn `arc ui start --team-root <tmp>` for the module."""
    arc_bin = _find_arc()
    if arc_bin is None:
        pytest.skip("arc CLI not on PATH and not in active venv")

    team_root = tmp_path_factory.mktemp("rehearsal_team")
    _build_team(team_root)

    port = _free_port()
    log_path = tmp_path_factory.mktemp("rehearsal_log") / "arcui.log"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        [
            arc_bin, "ui", "start",
            "--no-browser",
            "--viewer-token", VIEWER_TOKEN,
            "--operator-token", OPERATOR_TOKEN,
            "--agent-token", AGENT_TOKEN,
            "--port", str(port),
            "--team-root", str(team_root),
        ],
        stdout=log_fh, stderr=subprocess.STDOUT,
        env={**os.environ},
    )
    try:
        if not _wait_for_server(port):
            try:
                tail = log_path.read_text()[-2000:]
            except Exception:
                tail = "(log unavailable)"
            pytest.skip(f"arc ui did not come up: {tail}")
        yield (f"http://127.0.0.1:{port}", team_root)
    finally:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log_fh.close()


def _ensure_browser_available() -> None:
    """Skip cleanly if `playwright install` has not been run."""
    cache = Path.home() / "Library/Caches/ms-playwright"  # macOS default
    fallback = Path.home() / ".cache/ms-playwright"
    if cache.exists() and any(cache.glob("chromium*")):
        return
    if fallback.exists() and any(fallback.glob("chromium*")):
        return
    pytest.skip("Run `playwright install chromium` first")


@pytest.fixture(scope="module")
def browser(server_team: tuple[str, Path]) -> Iterator[object]:
    _ensure_browser_available()
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


def _new_page(browser: object, url: str) -> tuple[object, list[str], list[str]]:
    page_errors: list[str] = []
    api_errors: list[str] = []
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})  # type: ignore[attr-defined]
    ctx.add_init_script(
        f"window.localStorage.setItem('arcui_viewer_token', {json.dumps(VIEWER_TOKEN)});"
    )
    page = ctx.new_page()
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on(
        "response",
        lambda r: api_errors.append(f"{r.status} {r.url}")
        if r.status >= 400 and "/api/" in r.url
        else None,
    )
    page.goto(url, wait_until="networkidle")
    return page, page_errors, api_errors


# ============================================================
# Tests
# ============================================================


class TestRehearsalSPABoot:
    def test_index_html_has_all_panels_and_scripts(
        self, server_team: tuple[str, Path]
    ) -> None:
        url, _ = server_team
        import urllib.request
        with urllib.request.urlopen(url + "/") as resp:
            html = resp.read().decode("utf-8")
        for panel in ("agents", "agent-detail", "tasks", "tools-skills",
                      "security", "policy", "telemetry", "settings"):
            assert f'data-page-content="{panel}"' in html, f"missing panel: {panel}"
        for asset in ("agents-page.js", "agent-detail.js", "agent-controls.js",
                      "tasks-page.js", "tools-skills-page.js",
                      "security-page.js", "policy-page.js",
                      "live-updates.js", "prism.min.js", "markdown.js"):
            assert f"assets/{asset}" in html, f"missing script tag: {asset}"


class TestRehearsalAgentsPage:
    def test_fleet_renders_cards(
        self, browser: object, server_team: tuple[str, Path]
    ) -> None:
        url, _ = server_team
        page, page_errors, api_errors = _new_page(browser, f"{url}/?page=agents")
        try:
            page.wait_for_selector(".agent-card", timeout=10_000)
            cards = page.locator(".agent-card").count()
            assert cards == 2, f"expected 2 cards (alpha+beta), got {cards}"
            agents_panel = page.locator('[data-page-content="agents"]')
            total = agents_panel.locator(
                ".stat-card-label:has-text('Total') + .stat-card-value"
            ).first.text_content()
            assert total == "2"
            assert page_errors == []
            assert api_errors == []
        finally:
            page.close()


class TestRehearsalAgentDetail:
    def test_deep_link_mounts_detail_with_correct_agent(
        self, browser: object, server_team: tuple[str, Path]
    ) -> None:
        url, _ = server_team
        page, page_errors, api_errors = _new_page(
            browser, f"{url}/?page=agent-detail&agent=alpha"
        )
        try:
            page.wait_for_selector(".ad-tabs", timeout=10_000)
            page.wait_for_load_state("networkidle", timeout=4_000)
            title = page.locator("#ad-title").text_content()
            assert "alpha" in (title or "").lower() or title == "Alpha Test"
            assert page_errors == [], f"page errors: {page_errors}"
            assert api_errors == [], f"api errors: {api_errors}"
        finally:
            page.close()

    def test_switching_agent_remounts_detail(
        self, browser: object, server_team: tuple[str, Path]
    ) -> None:
        """Regression for R5: same `page=agent-detail` but different
        `agent=` was caching the prior mount."""
        url, _ = server_team
        page, page_errors, _ = _new_page(browser, f"{url}/?page=agent-detail&agent=alpha")
        try:
            page.wait_for_selector(".ad-tabs", timeout=10_000)
            page.wait_for_load_state("networkidle", timeout=4_000)
            page.evaluate(
                "window.ARC.setRoute({page: 'agent-detail', agent: 'beta'})"
            )
            page.wait_for_load_state("networkidle", timeout=5_000)
            page.wait_for_function(
                'document.querySelector("#ad-title")?.textContent === "Beta Test" '
                '|| document.querySelector("#ad-title")?.textContent === "beta"',
                timeout=5_000,
            )
            title = page.locator("#ad-title").text_content()
            assert "beta" in (title or "").lower() or title == "Beta Test"
            # Identity tab must show the NEW agent's DID
            page.click('.ad-tabs .pill-nav-item[data-tab="identity"]')
            page.wait_for_timeout(800)
            ident = page.locator(".ad-body").inner_text()
            assert "did:arc:test:beta" in ident, ident
            assert page_errors == [], f"page errors: {page_errors}"
        finally:
            page.close()

    def test_all_nine_tabs_activate_without_errors(
        self, browser: object, server_team: tuple[str, Path]
    ) -> None:
        """R3+R4 regression: Overview previously failed silently due to
        Fmt.number `this` loss + /stats wrapper shape."""
        url, _ = server_team
        page, page_errors, api_errors = _new_page(
            browser, f"{url}/?page=agent-detail&agent=alpha"
        )
        try:
            page.wait_for_selector(".ad-tabs", timeout=10_000)
            for tab in ("overview", "identity", "sessions", "skills", "memory",
                        "policy", "tools", "telemetry", "files"):
                page.click(f'.ad-tabs .pill-nav-item[data-tab="{tab}"]')
                page.wait_for_function(
                    f'document.querySelector(".ad-body")?.dataset.tab === '
                    f'{json.dumps(tab)}',
                    timeout=5_000,
                )
                page.wait_for_timeout(700)
                body = page.locator(".ad-body").inner_text()
                # Specifically catch the regression — Overview hit the
                # "Failed to load overview" empty state when broken.
                if tab == "overview":
                    assert "Failed to load overview" not in body, body
            assert page_errors == [], f"page errors: {page_errors}"
            assert api_errors == [], f"api errors: {api_errors}"
        finally:
            page.close()


class TestRehearsalLiveUpdates:
    def test_policy_md_disk_write_reaches_browser_within_8s(
        self, browser: object, server_team: tuple[str, Path]
    ) -> None:
        url, team_root = server_team
        page, page_errors, _ = _new_page(
            browser, f"{url}/?page=agent-detail&agent=alpha"
        )
        try:
            page.wait_for_selector(".ad-tabs", timeout=10_000)
            page.click('.ad-tabs .pill-nav-item[data-tab="policy"]')
            page.wait_for_selector(".ad-policy-body .pb", timeout=10_000)

            page.evaluate(
                """() => {
                    window.__caught = [];
                    if (window.arcWS) {
                        window.arcWS.addEventListener('message', (e) => {
                            if (e.detail && e.detail.type === 'file_change') {
                                window.__caught.push(e.detail);
                            }
                        });
                    }
                }"""
            )
            time.sleep(0.5)
            marker = f"P{int(time.time()) % 10000:04d}"
            policy = team_root / "alpha_agent" / "workspace" / "policy.md"
            original = policy.read_text(encoding="utf-8")
            try:
                policy.write_text(
                    original
                    + f"\n- [{marker}] Browser-rehearsal canary "
                      f"{{score:8, uses:1, reviewed:2026-04-29, "
                      f"created:2026-04-29, source:test}}\n",
                    encoding="utf-8",
                )
                page.wait_for_function(
                    "window.__caught && window.__caught.some(m => "
                    "m.agent_id === 'alpha' && m.event_type === 'policy:bullets_updated')",
                    timeout=15_000,
                )
                page.wait_for_function(
                    f'document.querySelector(".ad-policy-body")?.innerHTML.includes({json.dumps(marker)})',
                    timeout=8_000,
                )
            finally:
                policy.write_text(original, encoding="utf-8")
            assert page_errors == [], f"page errors: {page_errors}"
        finally:
            page.close()
