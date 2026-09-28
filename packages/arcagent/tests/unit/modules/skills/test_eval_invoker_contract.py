"""The skills module hands its model consumers a text-in/text-out invoker (SPEC-083 T-1229).

The arcskill improver (judge, reflector, code mutator, merger, suite generator) and the
turn-end outcome classifier all call ``llm.invoke(prompt: str) -> str``. A raw model
handle takes ``list[Message]`` and returns a response object, so handing it the model
directly sent a bare string as ``messages`` on every call — a real provider rejects
that, and skill improvement never reached a model.

The fake model here enforces the real provider message contract: anything other than a
list of Arc messages raises, exactly as a provider's request serializer would.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import arcrun
import pytest
from arcskill.improver.models import BundleView

from arcagent.modules.skills import _runtime


class _ProviderContractModel:
    """An eval model that accepts only what a real provider accepts."""

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    @property
    def name(self) -> str:
        return "contract"

    @property
    def model_name(self) -> str:
        return "contract/model"

    def validate_config(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def invoke(self, messages: Any, tools: Any = None, **_kw: Any) -> Any:
        if not isinstance(messages, list) or not all(
            isinstance(m, arcrun.Message) for m in messages
        ):
            raise TypeError(
                f"provider contract: messages must be list[Message], got {type(messages).__name__}"
            )
        self.requests.append(messages)
        return arcrun.LLMResponse(
            content="{}",
            stop_reason="end_turn",
            usage=arcrun.Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            model="contract/model",
        )

    def sent_text(self) -> str:
        return "\n".join(str(m.content) for request in self.requests for m in request)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ProviderContractModel]:
    fake = _ProviderContractModel()
    monkeypatch.setattr(_runtime, "get_eval_model", lambda **_kw: fake)
    _runtime.reset()
    yield fake
    _runtime.reset()


@pytest.mark.asyncio
async def test_improver_model_calls_reach_the_model_as_provider_messages(
    model: _ProviderContractModel, tmp_path: Path
) -> None:
    _runtime.configure(config={"adapter": "arcskill"}, workspace=tmp_path)
    merger = _runtime.state().adapter._merger  # production-constructed LLMSkillMerger
    assert merger is not None, "no eval model reached the improver"

    await merger.propose(
        a=BundleView(skill_name="invoice-a", text="# invoice-a\nBill the client."),
        b=BundleView(skill_name="invoice-b", text="# invoice-b\nBill the customer."),
        insight="duplicate skills",
    )

    assert model.requests, "the improver never called the model"
    assert "invoice-a" in model.sent_text()


@pytest.mark.asyncio
async def test_outcome_classifier_calls_reach_the_model_as_provider_messages(
    model: _ProviderContractModel, tmp_path: Path
) -> None:
    _runtime.configure(
        config={"adapter": "arcskill", "classify_outcomes": True}, workspace=tmp_path
    )
    classifier = _runtime.state().outcome_classifier
    assert classifier is not None

    await classifier.classify(
        transcript_window=[{"role": "user", "content": "thanks, that worked"}],
        active_skills=["demo"],
        error_counts={},
    )

    assert model.requests, "the outcome classifier never called the model"
    assert "thanks, that worked" in model.sent_text()
