"""Task 27 — CRITICAL: multi-agent process-wide runtime state bleed.

Live incident (DGX, 2026-07-10): a Telegram session addressed to josh_agent
(session_key correctly derived from josh's DID) produced a browserbase skill
signed with CODER_AGENT's DID — cryptographic proof the tool call that
created and signed that skill executed while holding coder_agent's actual
private key, not josh_agent's.

Root cause: ``arcagent.builtins.capabilities._runtime`` holds per-agent
context (workspace, identity, loader, audit_sink, tier, ...) as PLAIN
MODULE-LEVEL GLOBALS, configured once per ``ArcAgent.startup()`` call. Its
own docstring says "one agent process owns one set of values... if two
agents ever shared one process they would step on each other — but the
existing arcagent runtime model is single-agent-per-process, so this
matches." That assumption is FALSE for the embedded gateway (SPEC-023,
canonical at every tier per Josh's ruling): ``bootstrap._make_agent_factory``
+ ``arcui.embedded_agents._BoundedAgentCache`` keep up to 32 distinct
ArcAgent instances alive concurrently in ONE process. Every agent's
``.startup()`` re-runs ``_runtime.configure(...)``, silently overwriting the
globals for every OTHER already-loaded agent's in-flight tool calls.

Because ``SessionRouter.handle()`` spawns one ``asyncio.Task`` per session
and multiple sessions' tasks interleave on the same event loop (any
``await`` — an LLM call, a tool call — yields control), this is exploitable
purely by ordinary concurrent chat traffic: agent A's task is suspended
mid-turn (e.g. awaiting an LLM response) when agent B's task runs its own
``.startup()``/``configure()`` and clobbers the globals; when agent A's task
resumes and calls a builtin tool (write, create_skill, sign_artifact_file,
run_sandboxed_bash, ...), it reads agent B's workspace, identity (and thus
PRIVATE SIGNING KEY), audit sink, and tier — OWASP ASI03, Identity &
Privilege Abuse, in its most severe form.

These tests force real interleaving (per feedback_concurrency_tests_must_interleave
— sequential calls alone don't prove a concurrency bug) using asyncio.Event
to control exactly when each simulated agent's task yields.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from arctrust.identity import AgentIdentity

from arcagent.builtins.capabilities import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


def _identity(agent_type: str) -> AgentIdentity:
    return AgentIdentity.generate(org="arc", agent_type=agent_type)


class TestCrossAgentRuntimeBleed:
    """Reproduces the exact DGX incident mechanism with two interleaved
    simulated agent turns sharing one process (as the embedded gateway does).

    Before the contextvars fix, this scenario made josh's in-flight tool call
    observe coder's workspace and — critically — coder's private signing key,
    exactly matching the live incident (a browserbase skill created "during
    Josh's chat" but signed with coder_agent's DID). After the fix, each
    agent's ``asyncio.Task`` carries its own isolated runtime context, so a
    sibling task's ``configure()`` call is never visible.
    """

    @pytest.mark.asyncio
    async def test_interleaved_agents_stay_isolated(self, tmp_path: Path) -> None:
        """Agent A (josh) starts a turn, is suspended mid-flight (simulating an
        awaited LLM call). Agent B (coder) starts up and configures the shared
        runtime module with ITS OWN identity/workspace — running concurrently,
        in the SAME process, exactly as the embedded gateway's agent cache
        does. Agent A resumes and calls a builtin tool: it must observe ITS
        OWN workspace and identity, never coder's — the direct regression
        test for the DGX incident's cross-signed-artifact mechanism."""
        josh_identity = _identity("josh")
        coder_identity = _identity("coder")
        josh_workspace = tmp_path / "josh_agent" / "workspace"
        coder_workspace = tmp_path / "coder_agent" / "workspace"
        josh_workspace.mkdir(parents=True)
        coder_workspace.mkdir(parents=True)

        coder_started = asyncio.Event()
        josh_may_resume = asyncio.Event()

        observed: dict[str, object] = {}

        async def josh_turn() -> None:
            # Simulates ArcAgent.startup() for josh's session, running as its
            # own asyncio.Task (SessionRouter.handle() spawns one per session).
            _runtime.configure(workspace=josh_workspace, identity=josh_identity)
            # Simulates an in-flight await (e.g. an LLM call) — yields control
            # to the event loop, exactly as a real turn does mid-generation.
            coder_started.set()
            await josh_may_resume.wait()
            # Simulates josh's turn now calling a builtin tool, e.g.
            # create_skill's signing step or read/write's workspace() lookup.
            observed["workspace"] = _runtime.workspace()
            observed["identity_did"] = _runtime._identity_var.get().did

        async def coder_turn() -> None:
            await coder_started.wait()
            # Simulates ArcAgent.startup() for coder's session (its own
            # asyncio.Task), running concurrently while josh's turn is
            # suspended mid-await — the exact interleaving the live DGX
            # incident depended on.
            _runtime.configure(workspace=coder_workspace, identity=coder_identity)
            josh_may_resume.set()

        await asyncio.gather(josh_turn(), coder_turn())

        assert observed["workspace"] == josh_workspace, (
            "josh's tool call must see its OWN workspace, not coder's — a "
            "sibling task's configure() call must never leak across tasks."
        )
        assert observed["identity_did"] == josh_identity.did, (
            "josh's tool call must sign with its OWN identity — this is the "
            "exact mechanism that let coder_agent's private key sign an "
            "artifact during what was semantically Josh's Telegram turn."
        )

    @pytest.mark.asyncio
    async def test_sign_artifact_file_uses_own_identity_under_interleaving(
        self, tmp_path: Path
    ) -> None:
        """End-to-end through the real signing path (not just the raw
        contextvar read) — the exact tool-level call the incident's
        browserbase SKILL.md.arcsig went through."""
        josh_identity = _identity("josh")
        coder_identity = _identity("coder")
        artifact = tmp_path / "SKILL.md"
        artifact.write_bytes(b"---\nname: browserbase-browse\n---\n")

        coder_started = asyncio.Event()
        josh_may_resume = asyncio.Event()
        signed_ok: dict[str, bool] = {}

        async def josh_turn() -> None:
            _runtime.configure(workspace=tmp_path, identity=josh_identity)
            coder_started.set()
            await josh_may_resume.wait()
            signed_ok["result"] = _runtime.sign_artifact_file(artifact, artifact.read_bytes())

        async def coder_turn() -> None:
            await coder_started.wait()
            _runtime.configure(workspace=tmp_path, identity=coder_identity)
            josh_may_resume.set()

        await asyncio.gather(josh_turn(), coder_turn())

        assert signed_ok["result"] is True
        from arcagent.capabilities import artifact_signing

        manifest = artifact_signing.load_signature(artifact)
        assert manifest is not None
        assert manifest.signer_did == josh_identity.did, (
            "The artifact must be signed with josh's DID (whoever's turn "
            "actually called the tool), never coder's — the exact incident "
            "signature this task closes."
        )
