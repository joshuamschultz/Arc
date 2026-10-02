"""T-1203 (SPEC-083 COMP-023) — arcagent composes promotion; it never classifies.

RED intent: ``_runtime.configure`` hands the brain no promotion config and no
publisher, accepts no shared port, and ``memory.consolidated`` carries no sweep
status. The tests fail on those missing behaviors (``TypeError`` for the absent
``shared_knowledge`` parameter, assertion failures elsewhere).

Contract assumed (SDD COMP-023, README decision 10). Names marked * are not fixed
by the SDD and are this test's choice — the builder may rename both sides:

- *``_runtime.configure(..., shared_knowledge=<SharedKnowledgePort | None>)`` —
  the attached fleet port. (Today nothing in production ever sets
  ``_State.shared_knowledge``; see the report.)
- Only when ``promotion.enabled`` and tier != federal does configure pass the
  promotion settings to the selected backend's ``build_brain(context)`` —
  *``context["promotion_config"]`` (or ``backend_config["promotion"]``), a plain
  mapping. arcagent passes CONFIG, never an arcllm object; arcmemory builds the
  classifier.
- *``context["promotion_publisher"]`` is a ``SharedKnowledgePublisher`` when a
  shared port is attached, else ``None`` (the sweep then reports
  ``publisher_unavailable``).
- The composed publisher promotes with ``access.caller_did`` = the runtime DID.
- ``consolidate_poll_once`` never raises for promotion and forwards the brain's
  ``promotion_status`` on the ``memory.consolidated`` bus event.
- The memory module imports and runs with ``arcllm.classifiers.jev`` and
  ``typesafe_sdk`` absent, and no memory-module file imports arcllm.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import types
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from arcmemory.promotion.render import render_candidate
from arcmemory.stores.insight import InsightStore
from arcmemory.types import Insight
from arctrust import AgentIdentity

import arcagent.modules.memory as memory_pkg
from arcagent.knowledge import KnowledgeAccess, KnowledgeRef
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import consolidate_poll_once
from arcagent.modules.memory.config import MemoryConfig

_FAKE_BACKEND = "spec083_fake_brain_backend"
_IN_WINDOW = datetime(2026, 9, 27, 4, 30)  # hour 4: inside the default 3..6 window
_KEY_CANARY = "sk-ts-canary-7c1e0d"


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


class _StubBrain:
    def __init__(self, summary: Mapping[str, object] | None = None) -> None:
        self._summary = dict(summary or {"episode_summary": "slept"})

    async def capture(self, text: str, **_: Any) -> None:  # pragma: no cover
        return None

    async def retrieve(self, query: str, **_: Any) -> str:  # pragma: no cover
        return ""

    async def consolidate(self, **_: Any) -> dict[str, object]:
        return dict(self._summary)

    async def refresh_index(self, **_: Any) -> None:  # pragma: no cover
        return None

    async def rebuild_index(self, **_: Any) -> None:  # pragma: no cover
        return None


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A named memory backend whose ``build_brain`` records the context it gets."""
    contexts: list[dict[str, Any]] = []
    module = types.ModuleType(_FAKE_BACKEND)

    def build_brain(context: dict[str, Any]) -> _StubBrain:
        contexts.append(context)
        return _StubBrain()

    module.build_brain = build_brain  # type: ignore[attr-defined]  # reason: synthetic module
    monkeypatch.setitem(sys.modules, _FAKE_BACKEND, module)
    return contexts


class _FakeSharedPort:
    def __init__(self) -> None:
        self.promoted: list[tuple[Any, KnowledgeAccess, dict[str, Any]]] = []

    async def save(self, draft: Any, access: KnowledgeAccess) -> KnowledgeRef:  # pragma: no cover
        raise NotImplementedError

    async def read(self, reference: str, access: KnowledgeAccess) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def search(self, query: str, access: KnowledgeAccess) -> list[Any]:  # pragma: no cover
        return []

    async def promote(self, source: Any, access: KnowledgeAccess, **kwargs: Any) -> KnowledgeRef:
        self.promoted.append((source, access, kwargs))
        return KnowledgeRef(scope="shared", identifier="shared-1", digest=source.digest)

    async def revoke(self, reference: str, access: KnowledgeAccess) -> None:  # pragma: no cover
        return None


def _configure(
    workspace: Path,
    identity: AgentIdentity,
    *,
    promotion: dict[str, Any] | None = None,
    tier: str = "personal",
    shared_knowledge: Any = None,
) -> None:
    _runtime.configure(
        config={"brain": _FAKE_BACKEND, "tier": tier, "promotion": promotion or {}},
        workspace=workspace,
        agent_did=identity.did,
        identity=identity,
        shared_knowledge=shared_knowledge,
    )


def _promotion_config(context: Mapping[str, Any]) -> Any:
    config = context.get("promotion_config")
    if config is None:
        config = (context.get("backend_config") or {}).get("promotion")
    return config


def _identity() -> AgentIdentity:
    return AgentIdentity.generate("test", "promo")


# -- composition gate ----------------------------------------------------------


@pytest.mark.parametrize("tier", ["personal", "enterprise"])
def test_enabled_non_federal_passes_promotion_config_and_publisher_to_the_brain(
    tmp_path: Path, backend: list[dict[str, Any]], tier: str
) -> None:
    from arcagent.modules.memory.promotion import SharedKnowledgePublisher

    _configure(
        tmp_path,
        _identity(),
        promotion={"enabled": True, "classifier_model": "jev-1.13.0"},
        tier=tier,
        shared_knowledge=_FakeSharedPort(),
    )

    (context,) = backend
    config = _promotion_config(context)
    assert isinstance(config, Mapping)
    assert config["enabled"] is True
    assert config["classifier"] == "jev"
    assert config["classifier_model"] == "jev-1.13.0"
    assert isinstance(context["promotion_publisher"], SharedKnowledgePublisher)


def test_enabled_without_a_shared_port_gives_the_brain_no_publisher(
    tmp_path: Path, backend: list[dict[str, Any]]
) -> None:
    _configure(tmp_path, _identity(), promotion={"enabled": True})

    (context,) = backend
    assert isinstance(_promotion_config(context), Mapping)
    assert "promotion_publisher" in context
    assert context["promotion_publisher"] is None


def test_disabled_promotion_composes_nothing_even_with_a_shared_port(
    tmp_path: Path, backend: list[dict[str, Any]]
) -> None:
    _configure(tmp_path, _identity(), shared_knowledge=_FakeSharedPort())

    (context,) = backend
    assert not _promotion_config(context)
    assert context.get("promotion_publisher") is None


@pytest.mark.parametrize("tier", ["federal", " Federal "])
def test_federal_with_promotion_enabled_never_builds_a_brain(
    tmp_path: Path, backend: list[dict[str, Any]], tier: str
) -> None:
    with pytest.raises(ValueError, match="federal"):
        _configure(
            tmp_path,
            _identity(),
            promotion={"enabled": True},
            tier=tier,
            shared_knowledge=_FakeSharedPort(),
        )

    assert backend == []


def test_federal_disabled_composes_no_publisher_even_with_a_shared_port(
    tmp_path: Path, backend: list[dict[str, Any]]
) -> None:
    _configure(tmp_path, _identity(), tier="federal", shared_knowledge=_FakeSharedPort())

    (context,) = backend
    assert not _promotion_config(context)
    assert context.get("promotion_publisher") is None


def _walk(value: Any, seen: set[int] | None = None) -> list[Any]:
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return []
    seen.add(id(value))
    found = [value]
    if isinstance(value, Mapping):
        for item in value.values():
            found.extend(_walk(item, seen))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found.extend(_walk(item, seen))
    return found


def test_brain_context_carries_config_only_no_arcllm_object_and_no_key(
    tmp_path: Path, backend: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", _KEY_CANARY)

    _configure(
        tmp_path, _identity(), promotion={"enabled": True}, shared_knowledge=_FakeSharedPort()
    )

    (context,) = backend
    assert isinstance(_promotion_config(context), Mapping)
    for value in _walk(context):
        assert not type(value).__module__.startswith("arcllm"), type(value)
        assert _KEY_CANARY not in repr(value)


# -- the composed publisher ----------------------------------------------------


async def test_composed_publisher_promotes_as_the_runtime_identity(
    tmp_path: Path, backend: list[dict[str, Any]]
) -> None:
    identity = _identity()
    port = _FakeSharedPort()
    insight = Insight(id="acme-renewal", statement="Acme renewal closes at $42k/yr.", trigger="t")
    InsightStore(tmp_path).write(insight)
    _configure(tmp_path, identity, promotion={"enabled": True}, shared_knowledge=port)
    publisher = backend[0]["promotion_publisher"]

    shared_ref = await publisher.publish(
        "insight:acme-renewal",
        content_sha256=render_candidate(insight).content_sha256,
        confidence=0.97,
        classifier_version="jev-1.13.0",
        classification="UNCLASSIFIED",
    )

    assert shared_ref == "shared-1"
    ((_source, access, kwargs),) = port.promoted
    assert access.caller_did == identity.did
    assert kwargs["decision"] == "classifier_promote"


# -- consolidate_poll_once passes the sweep status through ---------------------


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((event, payload))


def _bind(brain: _StubBrain, bus: _RecordingBus, promotion_enabled: bool = True) -> None:
    _runtime.bind(
        _runtime._State(
            config=MemoryConfig(promotion={"enabled": promotion_enabled}),
            brain=brain,
            workspace=Path("."),
            telemetry=None,
            bus=bus,
            agent_did="did:arc:test:agent/poll0001",
            active=True,
        )
    )


def _consolidated(bus: _RecordingBus) -> dict[str, Any]:
    payloads = [payload for event, payload in bus.events if event == "memory.consolidated"]
    assert len(payloads) == 1
    return payloads[0]


@pytest.mark.parametrize(
    "status", ["publisher_unavailable", "classifier_unavailable", "classifier_error", "completed"]
)
async def test_poll_with_promotion_enabled_emits_sweep_status_and_never_raises(
    status: str,
) -> None:
    bus = _RecordingBus()
    summary = {"episode_summary": "slept", "promotion_status": status, "promotion_promoted": 0}
    _bind(_StubBrain(summary), bus)

    fired = await consolidate_poll_once(now_local=_IN_WINDOW)

    assert fired is True
    assert _consolidated(bus)["promotion_status"] == status


async def test_poll_without_a_sweep_reports_no_promotion_status() -> None:
    bus = _RecordingBus()
    _bind(_StubBrain({"episode_summary": "slept"}), bus, promotion_enabled=False)

    await consolidate_poll_once(now_local=_IN_WINDOW)

    assert _consolidated(bus)["promotion_status"] is None


# -- optional-extension absence + concern purity -------------------------------


def test_memory_module_imports_and_runs_with_jev_extension_absent(tmp_path: Path) -> None:
    """Fresh interpreter: the Jev drop-in and its SDK are blocked on their canonical
    paths before anything imports; the memory module still loads, composes a real
    arcmemory brain with promotion on, and runs a nightly poll without raising."""
    script = textwrap.dedent(
        f"""
        import asyncio, sys
        from datetime import datetime
        sys.modules["arcllm.classifiers.jev"] = None
        sys.modules["typesafe_sdk"] = None
        from arctrust.identity import AgentIdentity
        from arcagent.modules.memory import _runtime, capabilities, promotion  # noqa: F401
        identity = AgentIdentity.generate(org="default", agent_type="executor")
        _runtime.configure(
            config={{"brain": "arcmemory", "embed_backend": "none",
                     "promotion": {{"enabled": True}}}},
            workspace={str(tmp_path)!r},
            agent_did=identity.did,
            identity=identity,
        )
        assert _runtime.state().active
        asyncio.run(capabilities.consolidate_poll_once(now_local=datetime(2026, 9, 27, 4, 30)))
        assert sys.modules["arcllm.classifiers.jev"] is None
        assert sys.modules["typesafe_sdk"] is None
        print("MEMORY-OK")
        """
    )
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}

    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "MEMORY-OK" in proc.stdout


def test_memory_module_never_imports_arcllm_or_the_vendor_sdk() -> None:
    root = Path(memory_pkg.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [
                f"{path.name}: {name}"
                for name in names
                if name.split(".")[0] in {"arcllm", "typesafe_sdk"}
            ]

    assert offenders == []
