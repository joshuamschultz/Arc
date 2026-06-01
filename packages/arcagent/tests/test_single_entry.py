"""Architecture test (SPEC-027 D.3 / AC-2.4, AC-1.1) — one execution entry.

The agent exposes exactly one way to run: ``run`` (streaming, session-bound) plus
``run_collected`` (the collect wrapper callbacks bind) and ``session`` (the pool
accessor). The old fork — ``chat`` / ``run_async`` / ``chat_async`` /
``chat_stream`` and the ``set_agent_chat_fn`` / ``chat_fn`` callback plumbing —
must be gone from non-test source.
"""

from __future__ import annotations

from pathlib import Path

from arcagent.core.agent import ArcAgent

_SRC = Path(__file__).resolve().parent.parent / "src" / "arcagent"

# Deleted execution methods + callback plumbing — zero references in source.
_FORBIDDEN = (
    "def chat(",
    "def chat_async(",
    "def run_async(",
    "def chat_stream(",
    "set_agent_chat_fn",
    "_agent_chat_fn",
    '"chat_fn"',
    "run_async_fn",
    "chat_async_fn",
)


def test_no_legacy_entry_or_callback_references_in_source() -> None:
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for needle in _FORBIDDEN:
            if needle in text:
                offenders.append(f"{py.relative_to(_SRC)} contains {needle!r}")
    assert not offenders, "Legacy execution surface still referenced:\n" + "\n".join(offenders)


def test_agent_exposes_only_the_one_entry() -> None:
    """The single streaming entry + its collect wrapper + the session pool — no more."""
    assert hasattr(ArcAgent, "run")
    assert hasattr(ArcAgent, "run_collected")
    assert hasattr(ArcAgent, "session")
    for gone in ("chat", "chat_async", "run_async", "chat_stream"):
        assert not hasattr(ArcAgent, gone), f"ArcAgent.{gone} must be deleted"


def test_agent_ready_emits_single_run_fn() -> None:
    """agent:ready carries one callback (run_fn); the chat/async fns are gone."""
    source = (_SRC / "core" / "agent.py").read_text(encoding="utf-8")
    assert '"run_fn": self.run_collected' in source
    assert '"chat_fn"' not in source
    assert '"run_async_fn"' not in source


def test_no_deleted_agent_surface_in_sibling_packages() -> None:
    """Other surfaces (gateway, CLI, TUI) reference no deleted agent method (AC-2.4).

    Only the unambiguous deleted symbols are scanned — ``run_async``/``chat``
    collide with Textual's ``app.run_async`` and Slack's ``chat_postMessage``.
    """
    repo_root = Path(__file__).resolve().parents[3]
    unambiguous = ("agent.chat_stream", ".chat_stream(", "set_agent_chat_fn", "_agent_chat_fn")
    offenders: list[str] = []
    for pkg in ("arcgateway", "arccli", "arctui"):
        pkg_src = repo_root / "packages" / pkg / "src"
        if not pkg_src.is_dir():
            continue
        for py in pkg_src.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for needle in unambiguous:
                if needle in text:
                    offenders.append(f"{py.relative_to(repo_root)} contains {needle!r}")
    assert not offenders, "Deleted agent surface referenced in a sibling package:\n" + "\n".join(
        offenders
    )
