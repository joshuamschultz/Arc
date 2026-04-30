"""Phase 4 structural tests for vendored frontend assets (SPEC-022 phase 4).

Per the existing pattern in `test_browser_bootstrap.py`, browser-side JS modules
are validated by parsing the source for observable contracts rather than running
a full headless browser. When a JS bundler or jsdom runner is added later these
tests should migrate; for now structural assertions match what the rest of this
package does.

Covers tasks:
  4.1 — assets/prism.min.js + assets/prism.css present, expose Prism global
        with python/toml/json/javascript languages
  4.2 — assets/markdown.js exposes renderMarkdown(text) and escapes HTML
  4.3 — assets/file-tree.js exposes ARC.FileTree with mount/dispose
  4.4 — assets/policy-bullet.js exposes ARC.PolicyBullet.render
  4.5 — assets/event-drawer.js + assets/audit-viewer.js export their globals
  4.6 — arc-shell.js PAGES list matches SDD §5.1 + readRoute/setRoute/popstate
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ASSETS = (
    Path(__file__).resolve().parents[2] / "src" / "arcui" / "static" / "assets"
)


def _read(name: str) -> str:
    path = _ASSETS / name
    assert path.exists(), f"missing static asset: {name}"
    return path.read_text(encoding="utf-8")


# -------------------- 4.1 Prism --------------------


class TestPrismVendored:
    def test_prism_js_present_and_exposes_global(self) -> None:
        text = _read("prism.min.js")
        assert "Prism" in text
        # highlight() entry point is what file-tree.js calls.
        assert "highlight" in text

    def test_prism_supports_required_languages(self) -> None:
        text = _read("prism.min.js")
        for lang in ("python", "toml", "json", "javascript"):
            assert lang in text, f"prism.min.js must declare language: {lang}"

    def test_prism_css_present(self) -> None:
        text = _read("prism.css")
        # Minimal token classes that the highlighter emits.
        for cls in (".token", "comment", "string", "keyword", "number"):
            assert cls in text, f"prism.css must style token class: {cls}"


# -------------------- 4.2 Markdown --------------------


class TestMarkdownRenderer:
    def test_exposes_render_markdown(self) -> None:
        text = _read("markdown.js")
        assert "renderMarkdown" in text

    def test_escapes_html(self) -> None:
        text = _read("markdown.js")
        # We require a html-escape helper so user content can't inject markup.
        # The SDD §5.5 reference implementation calls the helper `escape`.
        assert "&amp;" in text and "&lt;" in text and "&gt;" in text

    def test_supports_core_markdown_features(self) -> None:
        text = _read("markdown.js")
        # Headings (h1-h6)
        assert "h1" in text or "h${" in text or "<h" in text
        # Code fences
        assert "```" in text or "pre" in text
        # Lists, blockquote, links, strong, em — implemented per SDD §5.5
        for token in ("ul", "blockquote", "<a", "strong", "em"):
            assert token in text, f"markdown.js missing feature: {token}"


# -------------------- 4.3 File Tree --------------------


class TestFileTreeComponent:
    def test_exposes_filetree_global(self) -> None:
        text = _read("file-tree.js")
        # Component lives under window.ARC namespace per shell convention.
        assert "FileTree" in text
        # Must expose a mount entry point
        assert "mount" in text or "init" in text

    def test_persists_expand_state_via_localstorage(self) -> None:
        text = _read("file-tree.js")
        # localStorage key namespaced per SDD §5.6: arcui:tree:<agent_id>:<path>
        assert "localStorage" in text
        assert "arcui:tree:" in text

    def test_uses_markdown_renderer_and_prism(self) -> None:
        text = _read("file-tree.js")
        # Calls into renderMarkdown for .md and Prism.highlight for code
        assert "renderMarkdown" in text
        assert "Prism" in text


# -------------------- 4.4 Policy Bullet --------------------


class TestPolicyBulletComponent:
    def test_exposes_policy_bullet_global(self) -> None:
        text = _read("policy-bullet.js")
        assert "PolicyBullet" in text
        assert "render" in text

    def test_score_tier_classes(self) -> None:
        text = _read("policy-bullet.js")
        # Tiered colors per acceptance criterion 9 — must distinguish tiers
        # by class name. Class names live in CSS (arc-platform.css extension).
        assert "score" in text


# -------------------- 4.5 Event drawer + audit viewer --------------------


class TestEventDrawerComponent:
    def test_exposes_event_drawer(self) -> None:
        text = _read("event-drawer.js")
        assert "EventDrawer" in text
        # Drawer must be openable/closable
        assert "open" in text and "close" in text


class TestAuditViewerComponent:
    def test_exposes_audit_viewer(self) -> None:
        text = _read("audit-viewer.js")
        assert "AuditViewer" in text
        # Must render audit rows (action, target, outcome, timestamp)
        for field in ("action", "outcome"):
            assert field in text, f"audit-viewer.js missing field: {field}"


# -------------------- 4.6 arc-shell.js extensions --------------------


_REQUIRED_PAGES = (
    "agents",
    "agent-detail",
    "telemetry",
    "security",
    "tools-skills",
    "tasks",
    "policy",
    "settings",
)


class TestArcShellPages:
    @pytest.mark.parametrize("page_id", _REQUIRED_PAGES)
    def test_page_in_pages_list(self, page_id: str) -> None:
        text = _read("arc-shell.js")
        # Match `id: 'agents'` or `id: "agents"`
        assert (
            f"id: '{page_id}'" in text or f'id: "{page_id}"' in text
        ), f"PAGES list must contain id={page_id!r}"

    def test_agent_detail_is_hidden(self) -> None:
        text = _read("arc-shell.js")
        # SDD §5.1: agent-detail entry has `hidden: true` because it's
        # navigated into by clicking an agent card, not via the sidebar.
        assert "hidden: true" in text


class TestArcShellRouter:
    def test_read_route_exists(self) -> None:
        text = _read("arc-shell.js")
        assert "readRoute" in text
        # Must read URLSearchParams
        assert "URLSearchParams" in text or "location.search" in text

    def test_set_route_exists(self) -> None:
        text = _read("arc-shell.js")
        assert "setRoute" in text
        # Must use history.pushState per SDD §5.2
        assert "pushState" in text

    def test_popstate_hook_registered(self) -> None:
        text = _read("arc-shell.js")
        assert "popstate" in text


# -------------------- Index wiring --------------------


_INDEX_HTML = (
    Path(__file__).resolve().parents[2] / "src" / "arcui" / "static" / "index.html"
)


class TestIndexHtmlScriptOrder:
    def test_phase4_scripts_loaded(self) -> None:
        text = _INDEX_HTML.read_text()
        # All Phase 4 modules must be loaded before the inline init script.
        for asset in (
            "assets/prism.min.js",
            "assets/markdown.js",
            "assets/file-tree.js",
            "assets/policy-bullet.js",
            "assets/event-drawer.js",
            "assets/audit-viewer.js",
        ):
            assert asset in text, f"index.html must load: {asset}"

    def test_prism_css_linked(self) -> None:
        text = _INDEX_HTML.read_text()
        assert "assets/prism.css" in text
