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
from arcagent.capabilities import artifact_signing
from arctrust.identity import AgentIdentity

from arccli.commands import ext


def _write_agent(tmp_path: Path, *, tier: str, brain: str) -> Path:
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n'
        f'[security]\ntier = "{tier}"\n'
        f'[modules.memory]\nenabled = true\n[modules.memory.config]\nbrain = "{brain}"\n'
        '[modules.skills]\nenabled = true\n[modules.skills.config]\nadapter = "arcskill"\n',
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
    try:
        with redirect_stdout(out):
            ext.ext_handler(["verify", "--agent", str(agent)])
    except SystemExit as exc:  # surface the refused selection; a bare exit is undebuggable
        pytest.fail(f"verify exited {exc.code}; output:\n{out.getvalue()}")
    assert "load-clean" in out.getvalue()


def test_verify_flags_unallowlisted_byo_above_personal(tmp_path: Path) -> None:
    # A dotted BYO brain that is NOT operator-allowlisted is refused at load above
    # personal — verify must report it and exit non-zero (federal change-control gate).
    agent = _write_agent(tmp_path, tier="enterprise", brain="evil.mod:Brain")
    with pytest.raises(SystemExit) as exc:
        ext.ext_handler(["verify", "--agent", str(agent)])
    assert exc.value.code == 1


_CAP_SRC = (
    "from arcagent.tools._decorator import tool\n\n\n"
    "@tool(description='hi', classification='read_only', capability_tags=['x'],\n"
    "      when_to_use='when needed', version='1.0.0')\n"
    "async def greet(arg: str) -> str:\n"
    "    return arg\n"
)


def _agent_with_identity(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Write an agent with a real DID + on-disk keys and return (dir, agent_identity)."""
    key_dir = tmp_path / "keys"
    agent_id = AgentIdentity.generate(org="blackarc", agent_type="executor")
    agent_id.save_keys(key_dir)
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\n[llm]\nmodel = "x/y"\n'
        f'[identity]\ndid = "{agent_id.did}"\nkey_dir = "{key_dir}"\n'
        '[security]\ntier = "enterprise"\n',
        encoding="utf-8",
    )
    (tmp_path / "capabilities").mkdir()
    return tmp_path, agent_id


def _greet_row(text: str) -> str:
    return next(line for line in text.splitlines() if "greet" in line)


def test_inspect_flags_wrongkey_signed_capability_as_unsigned(tmp_path: Path) -> None:
    """HIGH-1: a capability self-signed with a random key is labeled NOT verified — inspect
    pins the ``.arcsig`` to the agent DID key, so an attacker key does not read as signed."""
    agent_dir, _ = _agent_with_identity(tmp_path)
    cap = agent_dir / "capabilities" / "greet.py"
    cap.write_text(_CAP_SRC, encoding="utf-8")
    attacker = AgentIdentity.generate(org="evil", agent_type="executor")
    artifact_signing.write_signature(
        cap, cap.read_bytes(), signer_did=attacker.did, private_key=attacker.signing_seed
    )

    out = io.StringIO()
    with redirect_stdout(out):
        ext.ext_handler(["inspect", "--agent", str(agent_dir)])
    assert "unsigned" in _greet_row(out.getvalue())


def test_inspect_labels_agentsigned_capability_signed(tmp_path: Path) -> None:
    """HIGH-1 companion: the SAME capability, signed by the agent DID key, reads "signed"."""
    agent_dir, agent_id = _agent_with_identity(tmp_path)
    cap = agent_dir / "capabilities" / "greet.py"
    cap.write_text(_CAP_SRC, encoding="utf-8")
    artifact_signing.write_signature(
        cap, cap.read_bytes(), signer_did=agent_id.did, private_key=agent_id.signing_seed
    )

    out = io.StringIO()
    with redirect_stdout(out):
        ext.ext_handler(["inspect", "--agent", str(agent_dir)])
    assert "signed" in _greet_row(out.getvalue())
