"""Architecture test: nothing operator-owned or secret ships into a runtime.

``~/arc`` is BOTH the deploy's rsync source and the operator's tree: beside the
source sit ``config/`` (``arc.env``, the legacy credential file), ``state/``
(keys, stores), the fleet, ``.entire/`` and ``.claude/`` (session transcripts).
On the DGX (9c280994) every ``~/.arc/runtime/<v>/`` held a plaintext copy of the
operator's secrets and transcripts, because the rsync excluded only the fleet
and a bare ``.env``.

And the runtime's name (``BUILD_STAMP``) pruned every directory NAMED
``modules``, including ``packages/arcagent/src/arcagent/modules``, so a change
confined to it reused the running runtime's directory.

These tests RUN the script's own rsync command and stamp block on a scratch
tree, so they check what the deploy does, not how it is spelled.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = (_REPO / "scripts" / "deploy-node.sh").read_text(encoding="utf-8")

#: Paths in the operator tree that must never land in a runtime.
_OPERATOR_OWNED = ("config", "state", "team", ".entire", ".claude")

needs_tools = pytest.mark.skipif(
    shutil.which("rsync") is None or shutil.which("shasum") is None,
    reason="rsync and shasum are needed to run the deploy script's own commands",
)


def _rsync_block() -> str:
    start = _SCRIPT.index("rsync -a --delete")
    end = _SCRIPT.index('"$RUNTIME_DIR/"', start) + len('"$RUNTIME_DIR/"')
    return _SCRIPT[start:end]


def _stamp_block() -> str:
    start = _SCRIPT.index('BUILD_STAMP="$(')
    end = _SCRIPT.index('\n)"', start) + len('\n)"')
    return _SCRIPT[start:end]


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _operator_tree(root: Path) -> None:
    """A source tree with the operator's files beside it, as on the DGX."""
    _write(root / "pyproject.toml", 'version = "0.0.0"\n')
    _write(root / "packages/arcagent/src/arcagent/modules/mod.py", "A = 1\n")
    _write(root / "packages/arcui/src/arcui/team/view.py", "B = 1\n")
    _write(root / "config/arc.env", "ANTHROPIC_API_KEY=sk-secret\n")
    _write(root / "config/connections.env", "JIRA_API_TOKEN=secret\n")
    _write(root / "state/operator/key", "key\n")
    _write(root / "team/agent/identity.key", "key\n")
    _write(root / ".entire/session.jsonl", "{}\n")
    _write(root / ".claude/settings.local.json", "{}\n")
    _write(root / ".env", "TOKEN=secret\n")
    _write(root / "deploy/node.env", "TOKEN=secret\n")
    _write(root / "packages/arcllm/.env.example", "OPENAI_API_KEY=\n")


def _run(block: str, env: dict[str, str], tail: str = "") -> str:
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{block}\n{tail}"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@needs_tools
def test_the_runtime_rsync_ships_source_and_nothing_operator_owned(tmp_path: Path) -> None:
    source, runtime = tmp_path / "arc", tmp_path / "runtime"
    _operator_tree(source)
    runtime.mkdir()

    _run(_rsync_block(), {"REPO_ROOT": str(source), "RUNTIME_DIR": str(runtime)})

    for owned in _OPERATOR_OWNED:
        assert not (runtime / owned).exists(), f"{owned}/ shipped into the runtime"
    leaked = sorted(str(p.relative_to(runtime)) for p in runtime.rglob("*.env"))
    assert leaked == [], f"secret env files shipped into the runtime: {leaked}"
    # Package-internal directories that share a name with an excluded top-level
    # one still ship, and the non-secret example env does too.
    assert (runtime / "packages/arcagent/src/arcagent/modules/mod.py").exists()
    assert (runtime / "packages/arcui/src/arcui/team/view.py").exists()
    assert (runtime / "packages/arcllm/.env.example").exists()


@needs_tools
def test_the_stamp_changes_with_package_modules_and_not_with_operator_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "arc"
    _operator_tree(source)
    env = {"REPO_ROOT": str(source), "FLEET_DIR": "team"}

    def stamp() -> str:
        return _run(_stamp_block(), env, 'echo "$BUILD_STAMP"')

    before = stamp()
    _write(source / "packages/arcagent/src/arcagent/modules/mod.py", "A = 2\n")
    after_code = stamp()
    for owned in ("config/gateway.toml", "state/x.toml", "team/agent/agent.toml"):
        _write(source / owned, "changed = true\n")
    _write(source / ".claude/notes.md", "changed\n")
    _write(source / ".entire/notes.md", "changed\n")
    after_operator = stamp()

    assert before != after_code, "a change only in a package's modules/ reused the runtime name"
    assert after_code == after_operator, "operator-owned files changed the runtime name"


def test_the_deploy_fails_if_anything_operator_owned_reached_the_runtime() -> None:
    """The outcome check runs after the rsync and before the runtime is activated."""
    rsync = _SCRIPT.index("rsync -a --delete")
    activate = _SCRIPT.index('runtime activate "$RUNTIME_VERSION"')
    after_rsync = _SCRIPT[rsync:activate].split('"$RUNTIME_DIR/"', 1)[1]
    for owned in ("config", "state", ".entire", ".claude"):
        assert re.search(rf"(^|\s){re.escape(owned)}[\s;/]", after_rsync), (
            f"no post-rsync check that {owned} did not land in the runtime"
        )
    assert "'*.env'" in after_rsync, "no post-rsync check for *.env files in the runtime"
