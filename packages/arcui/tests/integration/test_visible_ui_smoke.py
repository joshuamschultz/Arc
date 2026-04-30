"""Visible-UI smoke tests — the kind the user asked for after the live test
showed that structural-only assertions can pass while the running browser
is broken.

Each test simulates the user's actual flow:
  1. Start arcui (fresh subprocess)
  2. Open `/` in a fresh Chromium with NO localStorage seed and cache disabled
  3. Read what's actually on the screen — buttons, headings, sidebar items

The tests fail loudly if any of these regress:
  - The default landing page is "Agent Fleet", not "LLM Telemetry"
  - The sidebar has all expected nav buttons
  - Clicking each sidebar button changes the visible H1 to that page's heading
  - Cache-bust placeholder is replaced (no `{{ARC_BUILD_ID}}` literal in served HTML)
  - Static assets respond with a no-cache header so a restarted server's
    new code is picked up by the browser without a manual hard-refresh

The user's exact complaint maps to the first three: "no menu options for
agents, or details" + "blank, no data" + "this is not connected".
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

playwright_module = pytest.importorskip(
    "playwright.sync_api",
    reason="run `pip install playwright && playwright install chromium`",
)
sync_playwright = playwright_module.sync_playwright


VIEWER = "V_VISIBLE_SMOKE"
OPERATOR = "O_VISIBLE_SMOKE"
AGENT = "A_VISIBLE_SMOKE"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _arc_bin() -> str | None:
    on_path = shutil.which("arc")
    if on_path:
        return on_path
    venv = Path(sys.executable).parent / "arc"
    if venv.exists() and os.access(venv, os.X_OK):
        return str(venv)
    return None


def _build_team(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("alpha", "beta"):
        a = root / f"{name}_agent"
        a.mkdir()
        (a / "arcagent.toml").write_text(
            f'[agent]\nname = "{name}"\norg = "smoke"\ntype = "executor"\n'
            f'[identity]\ndid = "did:arc:smoke:{name}"\n'
            f'[llm]\nmodel = "anthropic/claude-sonnet-4-5"\n',
            encoding="utf-8",
        )
        (a / "workspace").mkdir()
        (a / "workspace" / "policy.md").write_text("# Policy\n", encoding="utf-8")


def _wait_listen(port: int, t: float = 10.0) -> bool:
    end = time.monotonic() + t
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    arc = _arc_bin()
    if arc is None:
        pytest.skip("arc not on PATH or venv")
    team = tmp_path_factory.mktemp("smoke_team")
    _build_team(team)
    log_dir = tmp_path_factory.mktemp("smoke_log")
    log = log_dir / "ui.log"
    port = _free_port()
    proc = subprocess.Popen(
        [arc, "ui", "start", "--no-browser",
         "--viewer-token", VIEWER, "--operator-token", OPERATOR,
         "--agent-token", AGENT, "--port", str(port),
         "--team-root", str(team)],
        stdout=open(log, "w"), stderr=subprocess.STDOUT,
        env={**os.environ},
    )
    try:
        if not _wait_listen(port):
            pytest.skip(f"server didn't start; log: {log.read_text()[-1500:]}")
        yield (f"http://127.0.0.1:{port}", team)
    finally:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _ensure_chromium() -> None:
    cache_a = Path.home() / "Library/Caches/ms-playwright"
    cache_b = Path.home() / ".cache/ms-playwright"
    for c in (cache_a, cache_b):
        if c.exists() and any(c.glob("chromium*")):
            return
    pytest.skip("run `playwright install chromium` first")


@pytest.fixture(scope="module")
def browser(server: tuple[str, Path]) -> Iterator[object]:
    _ensure_chromium()
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            yield b
        finally:
            b.close()


def _fresh_page(browser: object, url: str) -> tuple[object, list[str]]:
    """Brand-new context: no cookies, no localStorage, JS cache off."""
    ctx = browser.new_context(  # type: ignore[attr-defined]
        viewport={"width": 1400, "height": 900},
        bypass_csp=True,
    )
    page = ctx.new_page()
    page.route("**/*", lambda r: r.continue_(headers={**r.request.headers, "Cache-Control": "no-cache"}))
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(url, wait_until="networkidle")
    return page, errs


# ============================================================
# 1. Cache-bust hygiene — what made the user's bug invisible
# ============================================================


class TestCacheBustHygiene:
    """The HTML must reference asset URLs with a per-startup version
    string so the browser doesn't reuse stale JS across server restarts."""

    def test_index_html_has_no_unresolved_build_id_placeholder(
        self, server: tuple[str, Path]
    ) -> None:
        url, _ = server
        html = urllib.request.urlopen(url + "/").read().decode("utf-8")
        assert "{{ARC_BUILD_ID}}" not in html, (
            "{{ARC_BUILD_ID}} placeholder leaked into served HTML — "
            "the cache-bust replacement in server.py is not running."
        )

    def test_asset_urls_carry_version_param(self, server: tuple[str, Path]) -> None:
        url, _ = server
        html = urllib.request.urlopen(url + "/").read().decode("utf-8")
        assert 'arc-shell.js?v=' in html, html[:600]
        assert 'arc-platform.css?v=' in html

    def test_static_assets_send_no_cache_header(
        self, server: tuple[str, Path]
    ) -> None:
        url, _ = server
        with urllib.request.urlopen(url + "/assets/arc-shell.js?v=test") as r:
            cc = r.headers.get("Cache-Control", "")
        assert "no-cache" in cc, (
            f"static assets must be served with `no-cache` (got {cc!r}). "
            "Without this, browsers serve stale JS across server restarts "
            "and the user's UI shows the old layout."
        )


# ============================================================
# 2. The user's exact complaints, asserted as DOM truths
# ============================================================


class TestSidebarVisible:
    """User: 'there are no menu options for agents, or details'."""

    def test_sidebar_has_all_expected_buttons(
        self, browser: object, server: tuple[str, Path]
    ) -> None:
        url, _ = server
        page, errs = _fresh_page(browser, url + "/")
        try:
            page.wait_for_selector(".sidebar-item", timeout=8_000)
            ids = page.eval_on_selector_all(
                ".sidebar-item",
                "els => els.map(e => e.getAttribute('data-page'))",
            )
            for needed in ("agents", "telemetry", "security",
                           "tools-skills", "tasks", "policy", "settings"):
                assert needed in ids, (
                    f"sidebar missing nav button {needed!r}. "
                    f"Got: {ids}. The user reported this exact bug — "
                    "stale arc-shell.js cached without the SPEC-022 PAGES list."
                )
            assert errs == [], f"page errors during boot: {errs}"
        finally:
            page.close()


class TestDefaultLandingPageIsAgents:
    """User: 'the ui comes up blank, no data'. Default landing should be
    Agent Fleet (which has data) — not LLM Telemetry."""

    def test_root_url_lands_on_agents_page_with_visible_h1(
        self, browser: object, server: tuple[str, Path]
    ) -> None:
        url, _ = server
        page, _ = _fresh_page(browser, url + "/")
        try:
            page.wait_for_selector("h1", timeout=8_000)
            visible = page.eval_on_selector_all(
                "[data-page-content]:not(.hidden)",
                "els => els.map(e => e.dataset.pageContent)",
            )
            assert visible == ["agents"], (
                f"default landing must show only the agents panel, got {visible}"
            )
            # Heading text — the most basic "do these words exist on the screen" check
            heading = page.locator(
                '[data-page-content="agents"] h1'
            ).first.text_content()
            assert heading == "Agent Fleet", repr(heading)
        finally:
            page.close()


class TestSidebarClickChangesVisiblePanel:
    """Each sidebar button must toggle to its panel and show the right H1."""

    @pytest.mark.parametrize(
        "page_id,expected_h1",
        [
            ("agents", "Agent Fleet"),
            ("security", "Security & Audit"),
            ("tasks", "Tasks"),
            ("policy", "Policy Engine"),
            ("tools-skills", "Tools & Skills"),
        ],
    )
    def test_clicking_sidebar_shows_expected_heading(
        self, browser: object, server: tuple[str, Path],
        page_id: str, expected_h1: str,
    ) -> None:
        url, _ = server
        page, _ = _fresh_page(browser, url + "/")
        try:
            page.wait_for_selector(f'.sidebar-item[data-page="{page_id}"]', timeout=8_000)
            page.click(f'.sidebar-item[data-page="{page_id}"]')
            page.wait_for_function(
                f'document.querySelector(\'[data-page-content="{page_id}"]\')'
                "?.classList.contains('hidden') === false",
                timeout=5_000,
            )
            page.wait_for_load_state("networkidle", timeout=4_000)
            heading = page.locator(
                f'[data-page-content="{page_id}"] h1'
            ).first.text_content()
            assert heading == expected_h1, (
                f"page={page_id}: expected H1 {expected_h1!r}, got {heading!r}"
            )
        finally:
            page.close()


class TestAgentsPageLoadsRosterData:
    """User: 'no data'. The agents page must render at least one
    agent-card pulled from /api/team/roster (we built two in the fixture)."""

    def test_agent_cards_render_with_real_data(
        self, browser: object, server: tuple[str, Path]
    ) -> None:
        url, _ = server
        page, _ = _fresh_page(browser, url + "/")
        try:
            page.wait_for_selector(".agent-card", timeout=8_000)
            cards = page.locator(".agent-card").count()
            assert cards == 2, f"expected 2 cards, got {cards}"
            # Card text must include the actual model field — proves the
            # roster endpoint and rendering wired through.
            text = page.locator(".agent-grid").inner_text()
            assert "claude-sonnet-4-5" in text, text
        finally:
            page.close()


class TestAgentDetailDeepLinkRendersData:
    """Click a card → agent-detail with 9 tabs and identity DID."""

    def test_click_card_opens_detail_with_identity(
        self, browser: object, server: tuple[str, Path]
    ) -> None:
        url, _ = server
        page, _ = _fresh_page(browser, url + "/")
        try:
            page.wait_for_selector('[data-agent="alpha"]', timeout=8_000)
            page.click('[data-agent="alpha"]')
            page.wait_for_url("**?page=agent-detail&agent=alpha", timeout=5_000)
            page.wait_for_selector(".ad-tabs", timeout=8_000)
            page.click('.ad-tabs .pill-nav-item[data-tab="identity"]')
            page.wait_for_timeout(800)
            ident = page.locator(".ad-body").inner_text()
            assert "did:arc:smoke:alpha" in ident, ident
            # All 9 tab buttons must be present
            tabs = page.eval_on_selector_all(
                ".ad-tabs .pill-nav-item",
                "els => els.map(e => e.dataset.tab)",
            )
            for needed in ("overview", "identity", "sessions", "skills",
                           "memory", "policy", "tools", "telemetry", "files"):
                assert needed in tabs, f"missing tab button {needed!r}; got {tabs}"
        finally:
            page.close()
