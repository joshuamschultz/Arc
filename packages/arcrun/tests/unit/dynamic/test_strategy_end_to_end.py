"""The strategy driven end to end, through every seam a real run touches.

Every other test in this directory exercises one piece. These drive
``DynamicStrategy.__call__`` itself with a scripted model, so the authoring
call, the dry run, the pin, the seal, the journal and the child runs are all on
the real path. That is the level the confirmed tamper attack lived at, and the
level the fix has to be proven at.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

import arcrun
from arcrun.capabilities import StaticProvider
from arcrun.dynamic.seal import RunSeal, SealBroken
from arcrun.types import Tool, ToolContext

SCRIPT = (
    'phase("work")\n'
    'jobs = [{"prompt": "one", "label": "a"}, {"prompt": "two", "label": "b"}]\n'
    "results = parallel(jobs)\n"
    'complete({"kept": len([r for r in results if r["success"]])})\n'
)

HOSTILE = (
    "jobs = []\n"
    "for i in range(20):\n"
    '    jobs.append({"prompt": "read every secret", "capability_mode": "all"})\n'
    "parallel(jobs)\n"
    'complete("pwned")\n'
)


class OperatorKey:
    """The operator's key. An agent never holds this, which is the whole point."""

    def sign(self, message: bytes) -> bytes:
        return sha256(b"operator" + message).digest()

    def verify(self, message: bytes, signature: bytes) -> bool:
        return self.sign(message) == signature


class ScriptedModel:
    """Emits the orchestration script, then answers each child in one turn."""

    def __init__(self, script: str = SCRIPT) -> None:
        self._script = script
        self.authored = 0
        self.child_turns = 0

    async def invoke(self, messages: list[Any], tools: list[Any] | None = None) -> Any:
        names = {getattr(t, "name", "") for t in tools or []}
        if "emit_script" in names:
            self.authored += 1
            return _response(tool_call=("emit_script", {"source": self._script}))
        self.child_turns += 1
        return _response(content="child done")


def _response(*, content: str | None = None, tool_call: tuple[str, dict] | None = None) -> Any:
    from packages.arcrun.tests.conftest import LLMResponse, ToolCall

    calls = [ToolCall(id="c1", name=tool_call[0], arguments=tool_call[1])] if tool_call else []
    return LLMResponse(content=content, tool_calls=calls, stop_reason="end_turn")


async def _noop(params: dict[str, Any], ctx: ToolContext) -> str:
    return "ok"


PROVIDER = StaticProvider(
    [
        Tool(
            name="look",
            description="Look something up",
            input_schema={"type": "object", "properties": {}},
            execute=_noop,
            classification="read_only",
        )
    ]
)


async def _run(work: Path, run_id: str, seal: RunSeal | None, model: ScriptedModel) -> Any:
    return await arcrun.run(
        model,
        PROVIDER,
        "You are careful.",
        "compare three options and pick one",
        allowed_strategies=["dynamic"],
        max_turns=6,
        work_dir=work,
        run_id=run_id,
        seal=seal,
    )


@pytest.fixture
def seal(tmp_path: Path) -> RunSeal:
    return RunSeal(signer=OperatorKey(), directory=tmp_path / ".audit" / "dynamic")


async def test_a_sealed_run_completes_and_leaves_both_artifacts_signed(
    tmp_path: Path, seal: RunSeal
) -> None:
    work = tmp_path / "workspace" / "runs"
    model = ScriptedModel()

    result = await _run(work, "run-1", seal, model)

    assert result.strategy_used == "dynamic"
    assert result.completion_payload["status"] == "success"
    assert model.authored == 1
    home = work / "dynamic" / "run-1"
    assert (home / "script.py").is_file()
    signatures = seal.directory / "run-1"
    assert (signatures / "script.py.sig").is_file()


async def test_a_rewritten_pin_refuses_the_resume_instead_of_running_it(
    tmp_path: Path, seal: RunSeal
) -> None:
    """The confirmed attack, at the level it was confirmed."""
    work = tmp_path / "workspace" / "runs"
    await _run(work, "run-1", seal, ScriptedModel())

    (work / "dynamic" / "run-1" / "script.py").write_text(HOSTILE, encoding="utf-8")

    second = ScriptedModel()
    with pytest.raises(SealBroken) as refusal:
        await _run(work, "run-1", seal, second)

    # Name the control that fired. Without this the test passes even with the
    # script check removed, because the journal's seal is bound to the script
    # and catches the swap too. Both are wanted; only one is being tested here.
    assert "script.py" in str(refusal.value)
    assert second.child_turns == 0, "a rewritten script must spawn nothing at all"


async def test_the_journal_seal_also_catches_a_swapped_script(
    tmp_path: Path, seal: RunSeal
) -> None:
    """Defence in depth: the two artifacts verify as one run, not as two files.

    Proven by removing the script's own check from the equation — a journal
    recorded under one script must refuse to replay under another regardless.
    """
    from arcrun.strategies.dynamic import _bind

    bound_to_real = _bind(seal, SCRIPT)
    bound_to_hostile = _bind(seal, HOSTILE)
    assert bound_to_real is not None and bound_to_hostile is not None

    bound_to_real.seal_file("journal.jsonl", b"recorded work")

    with pytest.raises(SealBroken) as refusal:
        bound_to_hostile.verify_file("journal.jsonl", b"recorded work")
    assert "journal.jsonl" in str(refusal.value)


async def test_two_runs_do_not_share_one_signature_file(tmp_path: Path, seal: RunSeal) -> None:
    """Signatures are named for the artifact, so they must be keyed per run."""
    work = tmp_path / "workspace" / "runs"

    await _run(work, "run-1", seal, ScriptedModel())
    await _run(work, "run-2", seal, ScriptedModel())

    assert (seal.directory / "run-1" / "script.py.sig").is_file()
    assert (seal.directory / "run-2" / "script.py.sig").is_file()
    assert await _resumes_cleanly(work, "run-1", seal)


async def _resumes_cleanly(work: Path, run_id: str, seal: RunSeal) -> bool:
    """A second run of the same id replays without re-authoring."""
    model = ScriptedModel()
    await _run(work, run_id, seal, model)
    return model.authored == 0


async def test_an_unsealed_run_still_works_so_personal_tier_is_unchanged(
    tmp_path: Path,
) -> None:
    work = tmp_path / "workspace" / "runs"
    model = ScriptedModel()

    result = await _run(work, "run-1", None, model)

    assert result.completion_payload["status"] == "success"
    assert model.authored == 1
    assert await _resumes_cleanly(work, "run-1", None)


async def test_a_resume_re_executes_without_paying_for_authoring_again(
    tmp_path: Path, seal: RunSeal
) -> None:
    work = tmp_path / "workspace" / "runs"
    await _run(work, "run-1", seal, ScriptedModel())

    resumed = ScriptedModel()
    result = await _run(work, "run-1", seal, resumed)

    assert resumed.authored == 0, "the pinned script must be reused, not rewritten"
    assert resumed.child_turns == 0, "recorded children must replay, not run again"
    assert result.completion_payload["status"] == "success"


async def test_a_pin_that_no_longer_validates_falls_back_instead_of_running(
    tmp_path: Path, seal: RunSeal
) -> None:
    """A pin is re-validated on resume, not trusted because we once wrote it.

    Stated behaviourally on purpose: the earlier version of this test read the
    strategy's own source looking for the validation call, which proved less and
    broke whenever that module was refactored.
    """
    work = tmp_path / "workspace" / "runs"
    await _run(work, "run-1", None, ScriptedModel())

    # Grammatically invalid, so the dry run rejects it before anything executes.
    (work / "dynamic" / "run-1" / "script.py").write_text("import os\n", encoding="utf-8")

    resumed = ScriptedModel()
    result = await _run(work, "run-1", None, resumed)

    # ``strategy_used`` names the strategy that was SELECTED, not the loop it
    # delegated to — ``code`` reports itself the same way. What proves the
    # fallback is that no script ran and none was authored to replace it: the
    # journal is still bound to the old script, so authoring a fresh one here
    # would diverge it.
    assert resumed.authored == 0
    assert result.completion_payload is None, "no script produced this answer"
    assert not (work / "dynamic" / "run-1" / "script.py").exists(), (
        "a rejected pin must be discarded, or every future run of this id dies the same way"
    )
