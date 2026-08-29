"""H-039: the NON-module config surface must list every option too.

``test_agent_config_template_completeness.py`` already guards ``[modules.*]``
against drift between a module's ``<Name>Config`` and the scaffold. This is
its companion for everything else an agent's three TOML files carry:
``arcagent.toml``'s own top-level sections, plus ``arcllm.toml``'s
``[llm]``/``[eval]``/``[budget]`` and the whole of ``arcrun.toml``. All of it
is now rendered straight off the Pydantic models by
``arcagent.utils.config_render`` rather than a hand-typed template, so these
tests are really pinning the generator's wiring — a field the generator
forgot to walk fails here, not three releases later when an operator goes
looking for a knob that was never written down.
"""

from __future__ import annotations

import tomllib
from typing import Any

import pytest
from arcagent.core.config import ArcAgentConfig, ArcRunConfig, BudgetConfig, EvalConfig, LLMConfig
from pydantic import BaseModel

from arccli.commands.agent._common import (
    _DEFAULT_ARCLLM_CONFIG,
    _DEFAULT_ARCRUN_CONFIG,
    render_agent_config,
)

# arcagent.toml's sibling files own these ArcAgentConfig fields (arcllm.toml:
# [llm]/[eval]/[budget]; arcrun.toml: arcrun at its own root) — they get their
# own completeness checks below, against their OWN files.
_SIBLING_FILE_FIELDS = frozenset({"llm", "eval", "budget", "arcrun"})
# `modules` is a dynamic dict guarded by test_agent_config_template_completeness.py.
_DYNAMIC_FIELDS = frozenset({"modules"})
# LLMConfig.modules is the same shape of dynamic dict, owned by the SEPARATE
# arcllm-module commented-surface generator (test_arcllm_surface.py).
_LLM_DYNAMIC_FIELDS = frozenset({"modules"})


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _declared_keys(model: type[BaseModel], *, exclude: frozenset[str] = frozenset()) -> set[str]:
    def walk(m: type[BaseModel], prefix: str, top: bool) -> set[str]:
        keys: set[str] = set()
        for name, field in m.model_fields.items():
            if top and name in exclude:
                continue
            path = f"{prefix}{name}"
            nested = _nested_model(field.annotation)
            if nested is not None:
                keys |= walk(nested, f"{path}.", False)
            else:
                keys.add(path)
        return keys

    return walk(model, "", True)


def _optional_keys(model: type[BaseModel], *, exclude: frozenset[str] = frozenset()) -> set[str]:
    """Fields defaulting to ``None`` — TOML has no null, so they document as comments."""

    def walk(m: type[BaseModel], prefix: str, top: bool) -> set[str]:
        keys: set[str] = set()
        for name, field in m.model_fields.items():
            if top and name in exclude:
                continue
            path = f"{prefix}{name}"
            nested = _nested_model(field.annotation)
            if nested is not None:
                keys |= walk(nested, f"{path}.", False)
            elif field.default is None:
                keys.add(path)
        return keys

    return walk(model, "", True)


def _flatten(table: dict[str, Any], prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in table.items():
        path = f"{prefix}{key}"
        keys.add(path)
        if isinstance(value, dict):
            keys |= _flatten(value, f"{path}.")
    return keys


def _commented_keys(text: str) -> set[str]:
    """Leaf names shown anywhere as ``# key = ...`` in the rendered text."""
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        body = stripped.lstrip("#").strip()
        name, sep, _ = body.partition("=")
        if sep and name.strip().isidentifier():
            keys.add(name.strip())
    return keys


def _present_and_documented(
    rendered: dict[str, Any],
    text: str,
    model: type[BaseModel],
    *,
    exclude: frozenset[str] = frozenset(),
) -> set[str]:
    present = _flatten({k: v for k, v in rendered.items() if k not in exclude})
    documented = _commented_keys(text)
    for optional in _optional_keys(model, exclude=exclude):
        if optional.rsplit(".", 1)[-1] in documented:
            present.add(optional)
    return present


@pytest.fixture(scope="module")
def scaffold() -> str:
    return render_agent_config(name="probe", did="did:arc:test:probe")


@pytest.fixture(scope="module")
def rendered(scaffold: str) -> dict[str, Any]:
    return tomllib.loads(scaffold)


def test_arcagent_toml_declares_every_top_level_field(
    rendered: dict[str, Any], scaffold: str
) -> None:
    exclude = _SIBLING_FILE_FIELDS | _DYNAMIC_FIELDS
    present = _present_and_documented(rendered, scaffold, ArcAgentConfig, exclude=exclude)
    declared = _declared_keys(ArcAgentConfig, exclude=exclude)
    missing = sorted(declared - present)
    assert not missing, (
        f"arcagent.toml is missing {missing} — every ArcAgentConfig field needs a line"
    )


def test_arcllm_toml_declares_every_llm_eval_budget_field() -> None:
    parsed = tomllib.loads(_DEFAULT_ARCLLM_CONFIG.split("# --- Per-agent")[0])
    present: set[str] = set()
    present |= _flatten({"llm": parsed.get("llm", {})})
    present |= _flatten({"eval": parsed.get("eval", {})})
    present |= _flatten({"budget": parsed.get("budget", {})})
    documented = _commented_keys(_DEFAULT_ARCLLM_CONFIG)

    declared: set[str] = set()
    for prefix, model, exclude in (
        ("llm.", LLMConfig, _LLM_DYNAMIC_FIELDS),
        ("eval.", EvalConfig, frozenset()),
        ("budget.", BudgetConfig, frozenset()),
    ):
        declared |= {f"{prefix}{key}" for key in _declared_keys(model, exclude=exclude)}
        for optional in _optional_keys(model, exclude=exclude):
            full = f"{prefix}{optional}"
            if optional.rsplit(".", 1)[-1] in documented:
                present.add(full)

    missing = sorted(declared - present)
    assert not missing, f"arcllm.toml is missing {missing}"


def test_arcrun_toml_declares_every_field() -> None:
    parsed = tomllib.loads(_DEFAULT_ARCRUN_CONFIG)
    present = _flatten(parsed)
    documented = _commented_keys(_DEFAULT_ARCRUN_CONFIG)
    declared = _declared_keys(ArcRunConfig)
    for optional in _optional_keys(ArcRunConfig):
        if optional.rsplit(".", 1)[-1] in documented:
            present.add(optional)
    missing = sorted(declared - present)
    assert not missing, f"arcrun.toml is missing {missing}"


def test_all_three_files_compose_through_the_real_config_model(tmp_path: Any) -> None:
    """The full 3-file surface parses AND validates end to end.

    Writes all three sibling files (not just arcagent.toml) so this exercises
    the actual composition path (``arcagent.core.config_loading.compose_raw_config``)
    a real ``arc agent create`` produces.
    """
    import arcagent

    agent_dir = tmp_path
    (agent_dir / "arcagent.toml").write_text(
        render_agent_config(name="probe", did="did:arc:test:probe"), encoding="utf-8"
    )
    (agent_dir / "arcllm.toml").write_text(_DEFAULT_ARCLLM_CONFIG, encoding="utf-8")
    (agent_dir / "arcrun.toml").write_text(_DEFAULT_ARCRUN_CONFIG, encoding="utf-8")

    config = arcagent.load_config(agent_dir / "arcagent.toml")

    assert config.agent.name == "probe"
    assert config.llm.max_tokens == 8192
    assert config.arcrun.max_turns == 120
