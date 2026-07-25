"""`arc agent build` renders the full config surface at a chosen tier.

`build` used to be an interactive wizard that wrote a truncated arcagent.toml:
it put `[llm]` in the wrong file (LLM-wire lives in arcllm.toml), hardcoded
`tier = "personal"`, showed a fraction of the operator-settable surface, and
blanked `[identity].did` — destroying the minted identity the agent signs its
capabilities with and registers to arcteam under.

It is now a deterministic template generator: pick a tier, get every option at
its default with a one-line note, keep your identity, and keep the LLM-wire
files it does not own.
"""

from __future__ import annotations

import io
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from arccli.commands.agent._dispatch import agent_handler


def _run(argv: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(argv)
    return out.getvalue()


def _parsed(agent_dir: Path) -> dict:
    return tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))


def _tiers_in(agent_dir: Path) -> set[str]:
    """Every `tier` value anywhere in the rendered config, at any nesting depth."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "tier" and isinstance(value, str):
                    found.add(value)
                walk(value)

    walk(_parsed(agent_dir))
    return found


class TestTierSelection:
    def test_defaults_to_personal(self, tmp_path: Path) -> None:
        _run(["build", str(tmp_path)])
        assert _tiers_in(tmp_path) == {"personal"}

    @pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
    def test_tier_flag_applies_to_every_subsystem(self, tmp_path: Path, tier: str) -> None:
        """One flag sets the tier everywhere — a config that is federal in
        [security] but personal in [modules.web] is a hole, not a preference."""
        _run(["build", str(tmp_path), "--tier", tier])
        assert _tiers_in(tmp_path) == {tier}

    def test_rejects_unknown_tier(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _run(["build", str(tmp_path), "--tier", "secret-squirrel"])


class TestFullSurface:
    def test_renders_the_whole_operator_surface(self, tmp_path: Path) -> None:
        _run(["build", str(tmp_path)])
        cfg = _parsed(tmp_path)
        # The point of the command: you can see what is possible without
        # reading the source.
        for section in ("agent", "identity", "ui", "context", "session", "security", "modules"):
            assert section in cfg, f"missing [{section}] in rendered template"
        for module in ("memory", "policy", "skills", "web", "browser", "scheduler"):
            assert module in cfg["modules"], f"missing [modules.{module}]"

    def test_no_llm_wire_in_arcagent_toml(self, tmp_path: Path) -> None:
        """`[llm]` belongs to arcllm.toml. Writing it here is what made
        `arc agent build --check` report a correctly configured agent as
        having no model."""
        _run(["build", str(tmp_path)])
        assert "llm" not in _parsed(tmp_path)

    def test_valid_toml_and_named_for_its_directory(self, tmp_path: Path) -> None:
        agent = tmp_path / "aria"
        agent.mkdir()
        _run(["build", str(agent)])
        assert _parsed(agent)["agent"]["name"] == "aria"


class TestRenderedConfigIsActuallyValid:
    """A template that does not load is worse than no template."""

    @pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
    def test_round_trips_through_the_real_config_model(self, tmp_path: Path, tier: str) -> None:
        from arcagent.core.config import load_config

        _run(["build", str(tmp_path), "--tier", tier])
        cfg = load_config(tmp_path / "arcagent.toml")
        assert cfg.security.tier == tier

    def test_federal_render_states_the_federal_crypto_floor(self, tmp_path: Path) -> None:
        """SecurityConfig refuses an explicitly weaker value fail-closed, and
        this template states every knob outright — so a federal render that
        emitted the personal defaults would produce a config that cannot load.
        """
        from arcagent.core.config import load_config

        _run(["build", str(tmp_path), "--tier", "federal"])
        security = load_config(tmp_path / "arcagent.toml").security
        assert security.require_fips is True
        assert security.custody == "vault_transit"
        assert security.signing_algorithm == "ecdsa-p256"


class TestLeavesLLMWireAlone:
    def test_provides_arcllm_toml_when_absent(self, tmp_path: Path) -> None:
        _run(["build", str(tmp_path)])
        arcllm = tmp_path / "arcllm.toml"
        assert arcllm.exists()
        # It must be a usable per-agent override, not an empty stub.
        assert "model" in tomllib.loads(arcllm.read_text(encoding="utf-8"))["llm"]

    def test_never_overwrites_an_existing_arcllm_toml(self, tmp_path: Path) -> None:
        """The per-agent arcllm.toml overrides the user-wide one. Regenerating
        the agent template must not silently discard that override."""
        arcllm = tmp_path / "arcllm.toml"
        arcllm.write_text('[llm]\nmodel = "openai/gpt-4o"\n', encoding="utf-8")
        _run(["build", str(tmp_path)])
        assert arcllm.read_text(encoding="utf-8") == '[llm]\nmodel = "openai/gpt-4o"\n'

    def test_never_overwrites_an_existing_arcrun_toml(self, tmp_path: Path) -> None:
        arcrun = tmp_path / "arcrun.toml"
        arcrun.write_text("max_turns = 7\n", encoding="utf-8")
        _run(["build", str(tmp_path)])
        assert arcrun.read_text(encoding="utf-8") == "max_turns = 7\n"


class TestNonDestructive:
    def test_refuses_to_clobber_existing_config_without_force(self, tmp_path: Path) -> None:
        original = '[agent]\nname = "aria"\n[identity]\ndid = "did:arc:local:executor/abc123"\n'
        (tmp_path / "arcagent.toml").write_text(original, encoding="utf-8")
        with pytest.raises(SystemExit):
            _run(["build", str(tmp_path)])
        assert (tmp_path / "arcagent.toml").read_text(encoding="utf-8") == original

    def test_force_preserves_the_minted_did(self, tmp_path: Path) -> None:
        """The DID is the identity the agent signs capabilities with and is
        registered to arcteam under. Regenerating the template must never
        blank it — that silently orphans the agent."""
        (tmp_path / "arcagent.toml").write_text(
            '[agent]\nname = "aria"\n[identity]\ndid = "did:arc:local:executor/abc123"\n',
            encoding="utf-8",
        )
        _run(["build", str(tmp_path), "--force", "--tier", "federal"])
        cfg = _parsed(tmp_path)
        assert cfg["identity"]["did"] == "did:arc:local:executor/abc123"
        assert cfg["agent"]["name"] == "aria"
        assert _tiers_in(tmp_path) == {"federal"}

    def test_scaffolds_workspace(self, tmp_path: Path) -> None:
        _run(["build", str(tmp_path)])
        assert (tmp_path / "workspace" / "identity.md").exists()
