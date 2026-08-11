"""Blueprint v2 loader — sibling config, file-tree prompts/capabilities/skills, schedules, questions.

v2 is additive: a blueprint that declares none of it keeps resolving with empty v2 fields.
A v2 blueprint folder may carry ``[arcllm]``/``[arcrun]`` tables, ``[[schedules]]``
and ``[[questions]]`` arrays, a ``prompts/<pkg>/<name>.md`` overlay tree, a
``capabilities/`` folder, and a ``skills/`` folder. Prompt overlays are validated
against the arcprompt catalog at resolve time — an unknown ``(package, name)`` is a
hard error (the producers-unwired failure mode: a typo must never silently no-op).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arccli import blueprints as bp


def _make_v2_blueprint(root: Path) -> Path:
    bp_dir = root / "sales-test"
    (bp_dir / "prompts" / "arcmemory").mkdir(parents=True)
    (bp_dir / "capabilities").mkdir()
    (bp_dir / "skills" / "deal-review").mkdir(parents=True)
    (bp_dir / "blueprint.toml").write_text(
        "[blueprint]\n"
        'name = "sales-test"\n'
        'version = "1.0.0"\n'
        'tier = "personal"\n\n'
        "[modules.memory]\n"
        "enabled = true\n\n"
        "[arcllm.llm]\n"
        'model = "anthropic/claude-sonnet-5"\n\n'
        "[arcrun]\n"
        "max_turns = 40\n\n"
        "[[schedules]]\n"
        'type = "cron"\n'
        'expression = "0 8 * * *"\n'
        'prompt = "Give me the morning pipeline briefing."\n\n'
        "[[questions]]\n"
        'id = "operator_name"\n'
        'prompt = "What is your name?"\n'
        'type = "text"\n',
        encoding="utf-8",
    )
    (bp_dir / "persona.md").write_text("You are a sales chief of staff.\n", encoding="utf-8")
    (bp_dir / "prompts" / "arcmemory" / "distill_fact.md").write_text(
        "Extract contacts, companies, and deals.\n", encoding="utf-8"
    )
    (bp_dir / "capabilities" / "crm.py").write_text("# crm verbs\n", encoding="utf-8")
    (bp_dir / "skills" / "deal-review" / "SKILL.md").write_text("stub\n", encoding="utf-8")
    return bp_dir


def test_v2_sibling_config_split(tmp_path: Path) -> None:
    bp_dir = _make_v2_blueprint(tmp_path)
    r = bp.resolve_blueprint(str(bp_dir), tier="personal")
    # arcllm/arcrun peeled out of the arcagent overlay.
    assert r.arcllm_overlay["llm"]["model"] == "anthropic/claude-sonnet-5"
    assert r.arcrun_overlay["max_turns"] == 40
    assert "arcllm" not in r.overlay and "arcrun" not in r.overlay
    # arcagent overlay keeps its own tables.
    assert r.overlay["modules"]["memory"]["enabled"] is True


def test_v2_schedules_and_questions(tmp_path: Path) -> None:
    r = bp.resolve_blueprint(str(_make_v2_blueprint(tmp_path)), tier="personal")
    assert r.schedules[0]["type"] == "cron"
    assert r.questions[0]["id"] == "operator_name"
    assert "schedules" not in r.overlay and "questions" not in r.overlay


def test_v2_discovers_prompt_capability_skill_trees(tmp_path: Path) -> None:
    r = bp.resolve_blueprint(str(_make_v2_blueprint(tmp_path)), tier="personal")
    names = {(p.package, p.name) for p in r.prompt_overlays}
    assert ("arcmemory", "distill_fact") in names
    body = next(p.body for p in r.prompt_overlays if p.name == "distill_fact")
    assert "deals" in body
    assert r.capabilities_dir is not None and r.capabilities_dir.is_dir()
    assert r.skills_dir is not None and (r.skills_dir / "deal-review").is_dir()


def test_v2_unknown_prompt_overlay_hard_errors(tmp_path: Path) -> None:
    bp_dir = tmp_path / "bad"
    (bp_dir / "prompts" / "arcmemory").mkdir(parents=True)
    (bp_dir / "blueprint.toml").write_text(
        '[blueprint]\nname = "bad"\nversion = "1.0.0"\ntier = "personal"\n',
        encoding="utf-8",
    )
    (bp_dir / "prompts" / "arcmemory" / "not_a_real_prompt.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not_a_real_prompt"):
        bp.resolve_blueprint(str(bp_dir), tier="personal")


def test_v1_blueprint_resolves_with_empty_v2_fields(tmp_path: Path) -> None:
    """Every v2 field is optional: absent means empty, never None and never a crash.

    Pinned to a synthetic v1 folder. This assertion used to ride on the shipped
    ``personal-assistant`` and broke the moment that blueprint grew the v2 sections it
    was always meant to have — "whichever shipped blueprint happens to still be simple"
    is a hostage of blueprint content, not a test of the loader.
    """
    bp_dir = tmp_path / "v1-only"
    bp_dir.mkdir()
    (bp_dir / "blueprint.toml").write_text(
        "[blueprint]\n"
        'name = "v1-only"\n'
        'version = "1.0.0"\n'
        'tier = "personal"\n\n'
        "[modules.memory]\n"
        "enabled = true\n",
        encoding="utf-8",
    )
    r = bp.resolve_blueprint(str(bp_dir), tier="personal")
    assert r.overlay["modules"]["memory"]["enabled"] is True  # v1 overlay passes through whole
    assert r.arcllm_overlay == {}
    assert r.arcrun_overlay == {}
    assert r.prompt_overlays == ()
    assert r.schedules == ()
    assert r.questions == ()
    assert r.capabilities_dir is None
    assert r.skills_dir is None


def test_shipped_personal_assistant_v2_sections_leave_the_arcagent_overlay() -> None:
    """The shipped blueprint's declared v2 tables reach their own fields, not ``overlay``.

    Structural, not value-pinned, so blueprint content can evolve. A declared
    ``[arcllm]``/``[arcrun]``/``[[schedules]]`` still sitting in ``overlay`` would be
    written into arcagent.toml and silently never applied — the producers-unwired
    failure mode, where the config reads correct and does nothing.
    """
    r = bp.resolve_blueprint("personal-assistant", tier="personal")
    assert r.arcllm_overlay["llm"]["model"]
    assert r.arcllm_overlay["budget"]["max_cost_usd"] > 0
    assert r.arcrun_overlay["max_turns"] > 0
    assert r.schedules and all(s["type"] == "cron" and s["expression"] for s in r.schedules)
    assert r.questions and all({"id", "prompt", "type"} <= set(q) for q in r.questions)
    for peeled in ("arcllm", "arcrun", "schedules", "questions"):
        assert peeled not in r.overlay
