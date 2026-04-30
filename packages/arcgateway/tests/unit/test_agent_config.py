"""Tests for arcgateway.agent_config — reads optional [ui] section from agent's arcagent.toml."""

from __future__ import annotations

from arcgateway.agent_config import UISection, load_ui_section


class TestPresent:
    def test_all_fields_present(self) -> None:
        cfg = {
            "agent": {"name": "alice"},
            "ui": {
                "display_name": "Alice (Curator)",
                "color": "#ff6b6b",
                "role_label": "policy curator",
                "hidden": True,
            },
        }
        ui = load_ui_section(cfg)
        assert ui.display_name == "Alice (Curator)"
        assert ui.color == "#ff6b6b"
        assert ui.role_label == "policy curator"
        assert ui.hidden is True

    def test_partial_fields(self) -> None:
        cfg = {"ui": {"color": "#abc"}}
        ui = load_ui_section(cfg)
        assert ui.color == "#abc"
        assert ui.display_name is None
        assert ui.role_label is None
        assert ui.hidden is False


class TestAbsent:
    def test_no_ui_section_returns_defaults(self) -> None:
        ui = load_ui_section({"agent": {"name": "x"}})
        assert ui.display_name is None
        assert ui.color is None
        assert ui.role_label is None
        assert ui.hidden is False

    def test_empty_dict(self) -> None:
        ui = load_ui_section({})
        assert ui.display_name is None
        assert ui.hidden is False

    def test_returns_UISection_instance(self) -> None:
        ui = load_ui_section({})
        assert isinstance(ui, UISection)


class TestWrongTypes:
    def test_ui_not_a_dict_yields_defaults(self) -> None:
        # If [ui] is somehow a list / string / int, fall back to defaults.
        for bad in ([], "string", 42, None):
            ui = load_ui_section({"ui": bad})  # type: ignore[dict-item]
            assert ui.display_name is None
            assert ui.color is None
            assert ui.hidden is False

    def test_hidden_truthy_coerced_to_bool(self) -> None:
        # We accept bool only; non-bool truthy values become False (don't trust string→bool magic).
        ui = load_ui_section({"ui": {"hidden": "yes"}})  # type: ignore[dict-item]
        assert ui.hidden is False

    def test_hidden_explicit_false(self) -> None:
        ui = load_ui_section({"ui": {"hidden": False}})
        assert ui.hidden is False

    def test_non_string_color_dropped(self) -> None:
        ui = load_ui_section({"ui": {"color": 12345}})  # type: ignore[dict-item]
        assert ui.color is None

    def test_non_string_display_name_dropped(self) -> None:
        ui = load_ui_section({"ui": {"display_name": ["alice"]}})  # type: ignore[dict-item]
        assert ui.display_name is None
