"""SPEC-047 Phase 5 — `arc ext inspect` / `arc ext verify` folded into the ext command.

Extension-point inspection was folded INTO the existing ``arc ext`` (WIRE-don't-rebuild)
rather than a colliding new top-level ``arc extensions`` (OQ-8). These exercise the CLI
handlers over a real flat-read config + a real CapabilityRegistry built from the builtins.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from arccli.commands import ext


def _write_agent(tmp_path: Path, *, tier: str, brain: str) -> Path:
    (tmp_path / "arcagent.toml").write_text(
        "[agent]\nname = \"aria\"\n[llm]\nmodel = \"x/y\"\n"
        f"[security]\ntier = \"{tier}\"\n"
        f"[modules.memory]\nenabled = true\n[modules.memory.config]\nbrain = \"{brain}\"\n"
        "[modules.skills]\nenabled = true\n[modules.skills.config]\nadapter = \"arcskill\"\n",
        encoding="utf-8",
    )
    return tmp_path


def test_inspect_renders_all_families(tmp_path: Path) -> None:
    agent = _write_agent(tmp_path, tier="personal", brain="arcmemory")
    out = io.StringIO()
    with redirect_stdout(out):
        ext.ext_handler(["inspect", "--agent", str(agent)])
    text = out.getvalue()
    assert "brain" in text and "arcmemory" in text
    assert "skills" in text and "arcskill" in text
    # scan-many tools from the real builtins registry appear too.
    assert "scan_many" in text


def test_verify_clean_at_personal(tmp_path: Path) -> None:
    agent = _write_agent(tmp_path, tier="personal", brain="arcmemory")
    out = io.StringIO()
    with redirect_stdout(out):
        ext.ext_handler(["verify", "--agent", str(agent)])
    assert "load-clean" in out.getvalue()


def test_verify_flags_unallowlisted_byo_above_personal(tmp_path: Path) -> None:
    # A dotted BYO brain that is NOT operator-allowlisted is refused at load above
    # personal — verify must report it and exit non-zero (federal change-control gate).
    agent = _write_agent(tmp_path, tier="enterprise", brain="evil.mod:Brain")
    with pytest.raises(SystemExit) as exc:
        ext.ext_handler(["verify", "--agent", str(agent)])
    assert exc.value.code == 1
