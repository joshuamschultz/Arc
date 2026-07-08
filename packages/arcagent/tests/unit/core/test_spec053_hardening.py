"""SPEC-053 hardening — custody + witness defects the separation depends on.

Covers the reachable-now defects found in review of the audit-authority split:

- #2a witness medium is relocated OUT of the operator-key directory.
- #2c startup witness-consistency check fails closed at federal on divergence.
- #2d federal witness submission is mandatory (fail closed, not fail open).
- #3  no silent operator-key regeneration when a prior audit chain exists.
- #1  the operator seed is not broadcast to the general module `available` dict.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest
from arctrust import OperatorKey, OperatorKeyIntegrityError, WitnessDivergenceError

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    TelemetryConfig,
)
from arcagent.core.model_manager import build_checkpoint_sink


def _config(tmp_path: Path, tier: str = "personal") -> ArcAgentConfig:
    cfg = ArcAgentConfig(
        agent=AgentConfig(name="op-agent", workspace=str(tmp_path / "ws")),
        llm=LLMConfig(model="test/model"),
        telemetry=TelemetryConfig(enabled=False),
    )
    cfg.security.operator_key_dir = str(tmp_path / "operator")
    cfg.security.witness_medium_path = str(tmp_path / "witness" / "anchor.log")
    cfg.security.tier = tier
    return cfg


# ---------------------------------------------------------------------------
# #2a — witness medium relocated out of the operator-key directory
# ---------------------------------------------------------------------------


def test_witness_medium_lives_outside_operator_key_dir(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tier="federal")
    agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
    witness = agent._build_witness()
    assert witness is not None
    medium = witness._path  # type: ignore[attr-defined]
    key_dir = Path(cfg.security.operator_key_dir).expanduser().resolve()
    assert key_dir not in medium.resolve().parents
    assert medium.resolve() == Path(cfg.security.witness_medium_path).expanduser().resolve()


def test_no_witness_below_federal(tmp_path: Path) -> None:
    agent = ArcAgent(config=_config(tmp_path, tier="personal"), config_path=tmp_path / "a.toml")
    assert agent._build_witness() is None


# ---------------------------------------------------------------------------
# #2c — startup witness-consistency check fails closed at federal on divergence
# ---------------------------------------------------------------------------


def _anchor_head(agent_root: Path, operator: OperatorKey, head: str) -> dict[str, Any]:
    checkpoint = {"head_hash": head, "record_count": 1, "files": ["t.jsonl"]}
    sink = build_checkpoint_sink(agent_root, operator.into_signer(), actor_did="did:arc:test:exec/a")
    sink(checkpoint)
    return checkpoint


def test_witness_divergence_detected_at_federal_fails_closed(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tier="federal")
    agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
    operator = OperatorKey.generate()
    agent._operator_key = operator
    agent._operator_signer = operator.into_signer()
    agent._witness = agent._build_witness()
    # A local operator-signed anchor exists, but the witness medium is empty —
    # exactly the state after a rollback + re-anchor by the operator-key holder.
    _anchor_head(agent._workspace.parent, operator, "d" * 64)
    with pytest.raises(WitnessDivergenceError):
        agent._verify_witness_consistency()


def test_witness_consistency_passes_when_head_witnessed(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tier="federal")
    agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
    operator = OperatorKey.generate()
    agent._operator_key = operator
    agent._operator_signer = operator.into_signer()
    witness = agent._build_witness()
    agent._witness = witness
    checkpoint = _anchor_head(agent._workspace.parent, operator, "e" * 64)
    assert witness is not None
    witness.submit(checkpoint, signature=b"\x00" * 64)
    agent._verify_witness_consistency()  # no raise


def test_fresh_federal_startup_has_no_false_divergence(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tier="federal")
    agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
    agent._operator_key = OperatorKey.generate()
    agent._operator_signer = agent._operator_key.into_signer()
    agent._witness = agent._build_witness()
    # Nothing anchored yet — a clean bootstrap must not fail closed.
    agent._verify_witness_consistency()


# ---------------------------------------------------------------------------
# #2d — federal witness submission is mandatory (fail closed)
# ---------------------------------------------------------------------------


class _FailingWitness:
    def submit(self, checkpoint: dict[str, Any], signature: bytes) -> str:
        raise OSError("witness medium unavailable")

    def verify_inclusion(self, checkpoint: dict[str, Any], proof: str) -> bool:
        return False


def test_federal_witness_submit_failure_fails_closed(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    operator = OperatorKey.generate()
    sink = build_checkpoint_sink(
        agent_root, operator.into_signer(), actor_did="did:arc:test:exec/a",
        witness=_FailingWitness(), federal=True,
    )
    with pytest.raises(OSError, match="witness medium unavailable"):
        sink({"head_hash": "f" * 64, "record_count": 1, "files": []})


def test_nonfederal_witness_submit_failure_is_swallowed(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    operator = OperatorKey.generate()
    sink = build_checkpoint_sink(
        agent_root, operator.into_signer(), actor_did="did:arc:test:exec/a",
        witness=_FailingWitness(), federal=False,
    )
    sink({"head_hash": "0" * 64, "record_count": 1, "files": []})  # no raise (AU-5)


# ---------------------------------------------------------------------------
# #3 — no silent operator-key regeneration when a prior chain exists
# ---------------------------------------------------------------------------


def test_missing_key_with_prior_chain_fails_startup_closed(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tier="personal")
    key_path = Path(cfg.security.operator_key_dir).expanduser() / "operator.key"
    OperatorKey.generate().save(key_path)
    agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
    # Simulate `rm operator.key operator.key.pub` while an audit chain survives.
    key_path.unlink()
    key_path.with_suffix(key_path.suffix + ".pub").unlink()
    chain = agent._policy_audit_log_path()
    chain.parent.mkdir(parents=True, exist_ok=True)
    chain.write_text('{"seq":0}\n', encoding="utf-8")
    assert agent._prior_audit_chains_exist() is True
    with pytest.raises(OperatorKeyIntegrityError):
        OperatorKey.load(
            agent._operator_key_path(),
            generate_if_absent=True,
            prior_chain_exists=agent._prior_audit_chains_exist(),
        )


# ---------------------------------------------------------------------------
# #1 — operator seed is not broadcast to the general module `available` dict
# ---------------------------------------------------------------------------


def _fake_agent_for_modules(tmp_path: Path, module_name: str, captured: dict[str, Any]) -> Any:
    cfg = _config(tmp_path)
    agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
    agent._operator_key = OperatorKey.generate()
    agent._operator_signer = agent._operator_key.into_signer()
    agent._identity = None
    agent._telemetry = None
    agent._bus = None
    agent._config.modules = {module_name: types.SimpleNamespace(enabled=True, config={})}  # type: ignore[assignment]

    def _fake_configure(*, operator_signer: Any = None, config: Any = None, **_: Any) -> None:
        captured["operator_signer"] = operator_signer

    fake_mod = types.SimpleNamespace(configure=_fake_configure)
    return agent, fake_mod


def test_operator_signer_not_offered_to_generic_module(tmp_path: Path, monkeypatch: Any) -> None:
    from arcagent.core import agent_lifecycle

    captured: dict[str, Any] = {}
    agent, fake_mod = _fake_agent_for_modules(tmp_path, "evil", captured)
    monkeypatch.setattr(agent_lifecycle.importlib, "import_module", lambda _name: fake_mod)
    agent_lifecycle.configure_module_runtimes(agent, agent._workspace)
    # A module NOT on the WORM-sink allowlist must never be handed signing
    # authority, even if its configure() declares an operator_signer parameter.
    assert captured["operator_signer"] is None


def test_operator_signer_offered_to_worm_sink_module(tmp_path: Path, monkeypatch: Any) -> None:
    from arcagent.core import agent_lifecycle

    captured: dict[str, Any] = {}
    agent, fake_mod = _fake_agent_for_modules(tmp_path, "skills", captured)
    monkeypatch.setattr(agent_lifecycle.importlib, "import_module", lambda _name: fake_mod)
    agent_lifecycle.configure_module_runtimes(agent, agent._workspace)
    # WORM-sink modules receive the config-resolved operator SIGNER (seedless
    # under vault_transit), never the raw key/seed.
    assert captured["operator_signer"] is agent._operator_signer
