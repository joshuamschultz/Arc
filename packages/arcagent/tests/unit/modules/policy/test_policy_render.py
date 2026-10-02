"""J2 F5/F6: the policy prompt section — pinned rules in full, learned bullets budgeted, no metadata."""

from __future__ import annotations

from arcagent.modules.policy.render import render_policy_section


def _bullet(i: int, score: int, text: str | None = None) -> str:
    body = text or f"learned lesson {i} about being careful with tools"
    return (
        f"- [P{i:02d}] {body} {{score:{score}, uses:{i}, "
        f"reviewed:2026-01-01, created:2026-01-01, source:s{i}}}"
    )


def test_curation_metadata_never_reaches_the_model() -> None:
    learned = "# Policy\n\n" + "\n".join(_bullet(i, 5) for i in range(1, 4))
    section = render_policy_section("", learned, max_tokens=4000)
    for leaked in ("score:", "uses:", "reviewed:", "created:", "source:", "[P0"):
        assert leaked not in section
    assert "learned lesson 2" in section


def test_highest_scored_bullets_survive_the_budget() -> None:
    learned = "\n".join(
        [_bullet(1, 3, "LOW"), _bullet(2, 9, "HIGH"), _bullet(3, 6, "MID")]
        + [_bullet(i, 4, "x" * 200) for i in range(4, 40)]
    )
    section = render_policy_section("", learned, max_tokens=60)
    assert "HIGH" in section
    assert "LOW" not in section
    assert "lower-scored learned rules omitted" in section
    assert len(section) / 4 <= 60 + 40  # budget plus the one-line omission note


def test_pinned_rules_render_in_full_before_learned_even_over_budget() -> None:
    pinned = "- " + "never do the forbidden thing " * 30
    section = render_policy_section(pinned, _bullet(1, 9), max_tokens=10)
    assert section.startswith("## Operator rules (pinned)")
    assert pinned.strip() in section
    assert "learned lesson 1" not in section  # the pinned text spent the whole budget


def test_pinned_only_has_no_learned_heading() -> None:
    section = render_policy_section("- always cite sources", "", max_tokens=4000)
    assert "always cite sources" in section
    assert "## Learned" not in section


def test_nothing_to_render_is_empty() -> None:
    assert render_policy_section("", "# Policy\n", max_tokens=4000) == ""
