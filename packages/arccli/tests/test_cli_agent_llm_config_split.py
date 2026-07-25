"""LLM-wire keys live in `arcllm.toml`, and the CLI must read them from there.

The three-file config split moved `[llm]` out of `arcagent.toml` (which is now
"everything EXCEPT LLM-wire", agent/_common.py) and into the agent's own
`arcllm.toml` — that is exactly what `arc agent create` scaffolds. But
`arc agent build --check` and `arc agent status` kept reading `[llm].model`
off `arcagent.toml`, so against a real scaffolded agent they reported
"No model configured" / "?" for a correctly configured agent.

`--check` returning FAIL is not cosmetic: it is the gate every automated
bootstrap runs before starting the service, so a container or node deploy
aborts on a healthy agent.

These fixtures deliberately mirror `arc agent create`'s real output — model in
`arcllm.toml`, absent from `arcagent.toml`.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from arccli.commands.agent._dispatch import agent_handler


def _write_split_agent(tmp_path: Path) -> Path:
    """Scaffold an agent in the layout `arc agent create` actually writes."""
    (tmp_path / "arcagent.toml").write_text(
        '[agent]\nname = "aria"\norg = "local"\ntype = "executor"\n',
        encoding="utf-8",
    )
    (tmp_path / "arcllm.toml").write_text(
        '[llm]\nmodel = "anthropic/claude-sonnet-5"\nmax_tokens = 8192\n',
        encoding="utf-8",
    )
    (tmp_path / "workspace").mkdir()
    return tmp_path


def _run(argv: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            agent_handler(argv)
        except SystemExit:
            pass  # a missing API key is a separate check; not what we assert here
    return out.getvalue()


def test_build_check_reads_model_from_arcllm_toml(tmp_path: Path) -> None:
    text = _run(["build", str(_write_split_agent(tmp_path)), "--check"])
    assert "model: anthropic/claude-sonnet-5" in text
    assert "No model configured" not in text


def test_status_reads_model_from_arcllm_toml(tmp_path: Path) -> None:
    text = _run(["status", str(_write_split_agent(tmp_path))])
    assert "anthropic/claude-sonnet-5" in text
