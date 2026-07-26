"""`arc agent create` scaffolds at a chosen tier, defaulting to personal.

`create` rendered the config template with the tier hardcoded to personal, so
the only way to stand up an enterprise or federal agent was to create a
personal one and regenerate it with `arc agent build --tier`. The tier is a
property of the agent from the moment it exists, not a later correction.

`--no-register` throughout: arcteam registration needs a live NATS broker and
is not what these assert.
"""

from __future__ import annotations

import io
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from arccli.commands.agent._dispatch import agent_handler


def _create(tmp_path: Path, name: str, *extra: str) -> Path:
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["create", name, "--dir", str(tmp_path), "--no-register", *extra])
    return tmp_path / name


def _tiers_in(agent_dir: Path) -> set[str]:
    """Every `tier` value anywhere in the rendered config, at any depth."""
    parsed = tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "tier" and isinstance(value, str):
                    found.add(value)
                walk(value)

    walk(parsed)
    return found


def test_defaults_to_personal(tmp_path: Path) -> None:
    assert _tiers_in(_create(tmp_path, "plain")) == {"personal"}


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
def test_tier_flag_applies_to_every_subsystem(tmp_path: Path, tier: str) -> None:
    assert _tiers_in(_create(tmp_path, f"a_{tier}", "--tier", tier)) == {tier}


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
def test_scaffold_loads_through_the_real_config_model(tmp_path: Path, tier: str) -> None:
    """A scaffold that cannot load is not a scaffold. Federal is the case that
    bites: SecurityConfig refuses an explicitly weaker crypto value fail-closed.
    """
    from arcagent.core.config import load_config

    agent_dir = _create(tmp_path, f"b_{tier}", "--tier", tier)
    assert load_config(agent_dir / "arcagent.toml").security.tier == tier


def test_federal_scaffold_states_the_federal_crypto_floor(tmp_path: Path) -> None:
    from arcagent.core.config import load_config

    agent_dir = _create(tmp_path, "fed", "--tier", "federal")
    security = load_config(agent_dir / "arcagent.toml").security
    assert security.require_fips is True
    assert security.custody == "vault_transit"
    assert security.signing_algorithm == "ecdsa-p256"


def test_reports_the_tier_it_scaffolded(tmp_path: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(
            ["create", "fedloud", "--dir", str(tmp_path), "--no-register", "--tier", "federal"]
        )
    assert "Tier: federal" in out.getvalue()


def test_suggested_next_step_actually_runs(tmp_path: Path) -> None:
    """`create` printed `arc agent build <dir>` as the next step, which now
    refuses on an existing config. A next step that errors is worse than none.
    """
    out = io.StringIO()
    with redirect_stdout(out):
        agent_handler(["create", "nextstep", "--dir", str(tmp_path), "--no-register"])
    suggested = [line.strip() for line in out.getvalue().splitlines() if "arc agent build" in line]
    assert suggested, "no `arc agent build` next step was printed"
    for line in suggested:
        assert line.endswith("--check"), f"suggested step would refuse: {line}"


def test_rejects_unknown_tier(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _create(tmp_path, "bogus", "--tier", "top-secret")


def test_llm_wire_still_goes_to_arcllm_toml(tmp_path: Path) -> None:
    """The tier flag must not disturb the three-file split."""
    agent_dir = _create(
        tmp_path, "split", "--tier", "federal", "--model", "anthropic/claude-opus-4-6"
    )
    agent_cfg = tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))
    llm_cfg = tomllib.loads((agent_dir / "arcllm.toml").read_text(encoding="utf-8"))
    assert "llm" not in agent_cfg
    assert llm_cfg["llm"]["model"] == "anthropic/claude-opus-4-6"
