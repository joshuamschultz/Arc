"""The ArcRun root is the complete model boundary for higher layers."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from arcllm.exceptions import ArcLLMAPIError

import arcrun


def test_model_contracts_are_available_from_root() -> None:
    message = arcrun.Message(role="user", content=[arcrun.TextBlock(text="hello")])
    tool = arcrun.ToolCall(id="1", name="search", arguments={})

    assert message.content[0].text == "hello"  # type: ignore[union-attr]
    assert tool.name == "search"
    assert arcrun.Model is arcrun.LLMProvider
    assert arcrun.ModelTool.__module__ == "arcllm.types"
    assert arcrun.Tool.__module__ == "arcrun.types"


def test_load_model_validates_modules_and_delegates() -> None:
    loaded = Mock(spec=arcrun.LLMProvider)
    with patch("arcrun.model.arcllm.load_model", return_value=loaded) as delegate:
        result = arcrun.load_model("test", "model", modules={"retry": True})

    assert result is loaded
    delegate.assert_called_once_with(
        "test",
        "model",
        budget_scope=None,
        on_event=None,
        trace_store=None,
        agent_label=None,
        agent_did=None,
        lineage=None,
        retry=True,
    )


def test_load_model_rejects_unknown_module_before_loading() -> None:
    with (
        patch("arcrun.model.arcllm.load_model") as delegate,
        pytest.raises(ValueError, match="Unknown model module key"),
    ):
        arcrun.load_model("test", modules={"qeue": True})
    delegate.assert_not_called()


def test_model_api_error_returns_safe_normalized_details() -> None:
    secret = "content_filter request included SECRET"
    error = ArcLLMAPIError(400, secret, "test-provider", retry_after=2.5)

    details = arcrun.model_api_error(error)

    assert details == arcrun.ModelAPIError(400, "test-provider", 2.5, "content_filtered")
    assert secret not in repr(details)
    assert arcrun.model_api_error(ValueError("not a model error")) is None


def test_model_identity_is_task_local_and_resets() -> None:
    from arcllm.modules import telemetry

    assert telemetry._agent_did_var.get() is None
    with arcrun.model_identity("did:arc:child", "child"):
        assert telemetry._agent_did_var.get() == "did:arc:child"
        assert telemetry._agent_label_var.get() == "child"
    assert telemetry._agent_did_var.get() is None


def test_create_model_trace_store_uses_agent_root(tmp_path: Path) -> None:
    store = arcrun.create_model_trace_store(tmp_path)

    assert callable(store.append)
    assert (tmp_path / "traces").is_dir()
