"""The eval agent's emitted config, read back — COMP-006 / REQ-183, REQ-185,
REQ-186, REQ-198, REQ-199.

Every requirement COMP-006 carries is a setting in a TOML file, and a setting
nothing reads back is how a benchmark ends up measuring something other than
what it claims to. So each one is asserted twice: once as the literal value in
the emitted file, and once through `load_config` — the effective value the
runtime will actually see after the three sibling files compose and the
user-wide layer merges under them.

Nothing here starts an agent, makes a network call, or writes outside
`tmp_path`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from evaluations.ingest.agent_factory import (
    ARCSTORE_DIR_NAME,
    eval_agent_name,
    pin_arcstore_data_dir,
    render_eval_agent_config,
    write_eval_agent_config,
)
from evaluations.ingest.limits import MAX_EVENT_CHARS, RECALL_BUDGET, RECALL_TOP_K

CONFIG_DIR = Path(__file__).resolve().parents[1] / "longmemeval" / "config"

# The run dir baked into the checked-in `.example` files. Obviously a
# placeholder: the real one is per-question and absolute.
EXAMPLE_RUN_DIR = Path("/absolute/path/to/evaluations/runs/lme-EXAMPLE")


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = (tmp_path / "runs" / "lme-q0001").resolve()
    path.mkdir(parents=True)
    return path


@pytest.fixture
def isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME and the Arc config root at throwaway dirs.

    The developer's own `~/.arc/arcagent.toml` merges under every per-agent
    config, so an unisolated `load_config` would read values this harness never
    wrote — and a stray key there could mask a setting we failed to emit.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-config"))
    return home


@pytest.fixture
def emitted(run_dir: Path) -> dict[str, Any]:
    """The parsed `arcagent.toml` for one question's agent."""
    config_path = write_eval_agent_config(question_id="q0001", run_dir=run_dir)
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


@pytest.fixture
def emitted_llm(run_dir: Path) -> dict[str, Any]:
    """The parsed `arcllm.toml` for one question's agent."""
    config_path = write_eval_agent_config(question_id="q0001", run_dir=run_dir)
    return tomllib.loads((config_path.parent / "arcllm.toml").read_text(encoding="utf-8"))


def _memory(config: dict[str, Any]) -> dict[str, Any]:
    section: dict[str, Any] = config["modules"]["memory"]["config"]
    return section


# ---------------------------------------------------------------------------
# T-798 — the six consolidation knobs and the recall envelope
# ---------------------------------------------------------------------------


def test_the_six_consolidation_settings_are_emitted_together(emitted: dict[str, Any]) -> None:
    """All six, or the run silently measures a brain that never consolidates.

    The outer three gate `consolidate_poll_once`; `dynamics` gates arcmemory's
    own persisted-stamp cadence inside `brain.consolidate()`. Lowering the outer
    three while the inner one sits at its 60-minute default makes every
    harness-driven pass after the first a no-op — and an empty `distill_provider`
    makes all five of the others theatre.
    """
    memory = _memory(emitted)

    assert memory["brain"] == "arcmemory"
    assert memory["distill_provider"] != ""
    assert memory["consolidate_event_threshold"] == 1
    assert memory["consolidate_idle_seconds"] == 0.0
    assert memory["consolidate_interval_seconds"] == 0.0
    assert memory["dynamics"]["consolidate_interval_minutes"] == 0.0


def test_the_recall_envelope_is_raised(emitted: dict[str, Any]) -> None:
    """At the default budget of 1024 exactly one recall survives enforce_budget."""
    memory = _memory(emitted)

    assert memory["top_k"] == RECALL_TOP_K == 20
    assert memory["budget"] == RECALL_BUDGET == 34_000


def test_the_sanitize_cap_is_raised_under_dynamics(emitted: dict[str, Any]) -> None:
    """The table matters as much as the value.

    `[modules.memory.config]` is a pydantic model with `extra="forbid"`, so
    `max_event_chars` there is a validation error the fail-open module loader
    turns into a NullBrain; `dynamics` is the opaque dict arcmemory's
    `build_brain` re-validates as its own config. Only the second reaches
    `sanitize`, and at arcmemory's own 2000 default 31.75% of this corpus's
    turns lose their tail with nothing raised.
    """
    memory = _memory(emitted)

    assert memory["dynamics"]["max_event_chars"] == MAX_EVENT_CHARS == 6000
    assert "max_event_chars" not in memory


def test_the_six_settings_survive_config_composition(
    run_dir: Path, isolated_arc_home: Path
) -> None:
    """The values the runtime sees, not just the ones on disk."""
    from arcagent.core.config import load_config

    config_path = write_eval_agent_config(question_id="q0001", run_dir=run_dir)
    memory = load_config(config_path).modules["memory"].config

    assert memory["brain"] == "arcmemory"
    assert memory["distill_provider"] != ""
    assert memory["consolidate_event_threshold"] == 1
    assert memory["consolidate_idle_seconds"] == 0.0
    assert memory["consolidate_interval_seconds"] == 0.0
    assert memory["dynamics"]["consolidate_interval_minutes"] == 0.0
    assert memory["dynamics"]["max_event_chars"] == MAX_EVENT_CHARS
    assert memory["top_k"] == RECALL_TOP_K
    assert memory["budget"] == RECALL_BUDGET


# ---------------------------------------------------------------------------
# T-799 — per-question isolation, tier, telemetry
# ---------------------------------------------------------------------------


def test_the_agent_name_carries_the_question_id(emitted: dict[str, Any]) -> None:
    assert emitted["agent"]["name"] == eval_agent_name("q0001") == "lme-q0001"


def test_no_identity_is_baked_into_the_config(emitted: dict[str, Any]) -> None:
    """T-796: the key mints lazily at startup rather than into ~/.arcagent/keys."""
    assert emitted["identity"]["did"] == ""


def test_the_tier_is_personal(emitted: dict[str, Any]) -> None:
    """`MemoryConfig.for_tier` changes write and decay dynamics, so the number
    reported is only meaningful with its tier attached (REQ-199)."""
    assert emitted["security"]["tier"] == "personal"
    assert _memory(emitted)["tier"] == "personal"


def test_the_policy_audit_log_is_workspace_relative(emitted: dict[str, Any]) -> None:
    """Empty routes the WORM chain to the shared arcstore worm dir, where 500
    throwaway agents contend on one exclusive flock and outlive their run dir."""
    configured = emitted["security"]["policy_audit_log"]

    assert configured != ""
    assert not Path(configured).is_absolute()


def test_the_policy_audit_log_resolves_inside_the_workspace(
    run_dir: Path, isolated_arc_home: Path
) -> None:
    """The relative value above, run through the real resolution rule."""
    from arcagent.core.agent import ArcAgent
    from arcagent.core.config import load_config

    config_path = write_eval_agent_config(question_id="q0001", run_dir=run_dir)
    agent = ArcAgent(load_config(config_path), config_path=config_path)

    assert agent._policy_audit_log_path().is_relative_to(run_dir)


def test_the_arcstore_data_dir_is_per_run(emitted: dict[str, Any], run_dir: Path) -> None:
    assert emitted["arcstore"]["data_dir"] == str(run_dir / ARCSTORE_DIR_NAME)


def test_pin_arcstore_data_dir_overrides_an_ambient_export(
    run_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var outranks the emitted TOML value, so it has to be set too —
    otherwise an operator shell exporting it pools all 500 runs into one DB."""
    from arcstore.config import ENV_DATA_DIR, resolve_data_dir

    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "someone-elses-store"))

    pinned = pin_arcstore_data_dir(run_dir)

    assert pinned == run_dir / ARCSTORE_DIR_NAME
    assert resolve_data_dir() == pinned
    assert pinned.is_dir()


def test_raw_body_capture_is_off(emitted_llm: dict[str, Any]) -> None:
    """The default persists every haystack chunk verbatim as plaintext JSONL
    inside the repo tree — several GB across a full run (REQ-186)."""
    assert emitted_llm["llm"]["modules"]["telemetry"]["store_raw_bodies"] is False


def test_raw_body_capture_survives_config_composition(
    run_dir: Path, isolated_arc_home: Path
) -> None:
    from arcagent.core.config import load_config

    config_path = write_eval_agent_config(question_id="q0001", run_dir=run_dir)

    assert load_config(config_path).llm.modules["telemetry"]["store_raw_bodies"] is False


def test_workpad_and_policy_stay_enabled(emitted: dict[str, Any]) -> None:
    """A recorded operator decision, not an oversight: full production fidelity.

    Both write into the system prompt, so the measurement scope is arcmemory
    plus two further LLM summarizers — which is why REQ-200 makes the manifest
    declare it rather than leaving it to be inferred.
    """
    assert emitted["modules"]["workpad"]["enabled"] is True
    assert emitted["modules"]["policy"]["enabled"] is True


def test_the_nats_backed_modules_are_off(emitted: dict[str, Any]) -> None:
    """No broker runs for a local benchmark, and a per-question throwaway must
    not announce itself to a fleet (SDD: NATS deliberately not integrated)."""
    assert emitted["modules"]["messaging"]["enabled"] is False
    assert emitted["modules"]["tasks"]["enabled"] is False


# ---------------------------------------------------------------------------
# T-796 — what the scaffold writes, and what it must not
# ---------------------------------------------------------------------------


def test_all_three_sibling_configs_are_written(run_dir: Path) -> None:
    """`load_config` composes three files from one directory; a missing sibling
    silently falls back to packaged defaults instead of failing."""
    config_path = write_eval_agent_config(question_id="q0001", run_dir=run_dir)

    assert config_path == run_dir / "agent" / "arcagent.toml"
    assert (config_path.parent / "arcllm.toml").is_file()
    assert (config_path.parent / "arcrun.toml").is_file()


@pytest.mark.parametrize(
    "relative",
    ["identity.md", "policy.md", "context.md", "sessions", "capabilities"],
)
def test_the_workspace_is_scaffolded(run_dir: Path, relative: str) -> None:
    config_path = write_eval_agent_config(question_id="q0001", run_dir=run_dir)

    assert (config_path.parent / "workspace" / relative).exists()


def test_a_relative_run_dir_is_refused(tmp_path: Path) -> None:
    """A relative run dir puts the arcstore back under the process CWD — the
    repo root — which is the leak this component exists to close."""
    with pytest.raises(ValueError, match="absolute"):
        render_eval_agent_config(question_id="q0001", run_dir=Path("evaluations/runs/x"))


# ---------------------------------------------------------------------------
# The checked-in templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["arcagent.toml.example", "arcllm.toml.example", "arcrun.toml.example"]
)
def test_the_checked_in_example_parses(name: str) -> None:
    tomllib.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


def test_the_checked_in_example_still_matches_what_a_run_emits() -> None:
    """The templates are generated, so they can drift the moment a value moves.

    Compared on the eval-critical values rather than byte for byte: an upstream
    comment reword in the scaffold template is not a reason to fail a build, but
    a dropped consolidation knob is.
    """
    fresh = tomllib.loads(render_eval_agent_config(question_id="EXAMPLE", run_dir=EXAMPLE_RUN_DIR))
    checked_in = tomllib.loads((CONFIG_DIR / "arcagent.toml.example").read_text(encoding="utf-8"))

    assert _memory(checked_in) == _memory(fresh)
    assert checked_in["security"] == fresh["security"]
    assert checked_in["arcstore"] == fresh["arcstore"]
    assert checked_in["agent"]["name"] == fresh["agent"]["name"]


def test_the_checked_in_llm_example_still_disables_raw_bodies() -> None:
    checked_in = tomllib.loads((CONFIG_DIR / "arcllm.toml.example").read_text(encoding="utf-8"))

    assert checked_in["llm"]["modules"]["telemetry"]["store_raw_bodies"] is False
