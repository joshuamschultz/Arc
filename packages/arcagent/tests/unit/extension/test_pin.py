"""ArtifactPinVerifier — the approved build, re-checked before every execution (COMP-009).

REQ-290 has three clauses and each one is asserted separately, because passing two of
them is a supply-chain hole rather than two-thirds of a control:

* *exact version and integrity hash* — a build that reports the pinned version but
  hashes differently is the interesting attack, so version and hash are separate
  refusals with separate audit reasons.
* *before each execution, not only at install* — asserted by verifying the same
  artifact twice with a mutation in between. A verifier that caches its verdict, or a
  launcher that checks once at attach time, passes the first assertion and fails here.
* *refuse and emit an audit event naming expected and actual* — asserted against the
  event that reached the sink, not against the exception text. An auditor reconstructs
  the refusal from the chain; a message only the operator saw is not a control.

Fetch-at-launch is asserted structurally, by reading the module's own imports. A
verifier that could reach the network or spawn a process could satisfy a mismatch by
downloading the expected bytes, which is exactly what makes a bare ``npx -y <package>``
invocation impossible to declare here.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.manifest import ArtifactPin, PlatformArtifact
from arcagent.extension.platforms import host_platform

#: A child that outlives the assertions in the launcher-enforcement tests.
_STAY_ALIVE = "import time; time.sleep(30)"

#: Anything that could turn "the bytes are wrong" into "let me go get the right bytes".
_FETCH_CAPABLE = frozenset(
    {"asyncio", "httpx", "requests", "shutil", "socket", "subprocess", "urllib"}
)

_CALLER = "did:arc:test-caller"


class _RecordingSink:
    """An audit sink that keeps what it was handed, so tests read the real event."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _module() -> ModuleType:
    import arcagent.extension.pin as module

    return module


def _launcher() -> ModuleType:
    import arcagent.extension.launcher as module

    return module


def _verifier(sink: _RecordingSink) -> Any:
    return _module().ArtifactPinVerifier(audit_sink=sink, tier=Tier.PERSONAL)


def _write_artifact(directory: Path, body: bytes = b"the approved build") -> Path:
    path = directory / "server.tgz"
    path.write_bytes(body)
    return path


def _pin(digest: str, *, version: str = "2.3.1", platform: str | None = None) -> ArtifactPin:
    """A pin covering exactly one platform — this host's, unless a test names another."""
    return ArtifactPin(
        package="example-mcp-server",
        version=version,
        platforms={
            platform or host_platform(): PlatformArtifact(
                url="https://example.invalid/example-mcp-server.tgz", sha256=digest
            )
        },
    )


def _artifact(path: Path, *, version: str = "2.3.1", installed: str | None = None) -> Any:
    """A pinned artifact whose pin matches ``path`` unless a test breaks it."""
    module = _module()
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "0" * 64
    return module.PinnedArtifact(
        pin=_pin(digest, version=version),
        path=path,
        installed_version=installed if installed is not None else version,
    )


def _denials(sink: _RecordingSink) -> list[AuditEvent]:
    return [event for event in sink.events if event.outcome == "deny"]


# --- the approved build passes, and says so on the record --------------------


def test_the_approved_build_runs(tmp_path: Path) -> None:
    sink = _RecordingSink()

    _verifier(sink).verify(_artifact(_write_artifact(tmp_path)), caller_did=_CALLER)

    assert _denials(sink) == []


def test_a_passing_verification_is_still_recorded(tmp_path: Path) -> None:
    """Pillar 4 has no happy-path exemption: an auditor must see what was allowed to run."""
    sink = _RecordingSink()

    _verifier(sink).verify(_artifact(_write_artifact(tmp_path)), caller_did=_CALLER)

    assert [event.outcome for event in sink.events] == ["allow"]
    assert sink.events[0].actor_did == _CALLER


# --- exact hash --------------------------------------------------------------


def test_a_changed_artifact_is_refused(tmp_path: Path) -> None:
    """The bytes on disk moved after approval — the classic post-install swap."""
    path = _write_artifact(tmp_path)
    artifact = _artifact(path)
    path.write_bytes(b"something else entirely")

    with pytest.raises(ExtensionError):
        _verifier(_RecordingSink()).verify(artifact, caller_did=_CALLER)


def test_the_refusal_event_names_the_expected_and_the_actual_hash(tmp_path: Path) -> None:
    """A refusal an auditor cannot reconstruct is not the control REQ-290 asks for."""
    sink = _RecordingSink()
    path = _write_artifact(tmp_path)
    artifact = _artifact(path)
    path.write_bytes(b"something else entirely")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(ExtensionError):
        _verifier(sink).verify(artifact, caller_did=_CALLER)

    (event,) = _denials(sink)
    covered = artifact.pin.for_host(host_platform())
    assert covered is not None
    assert event.extra["expected"] == covered.sha256
    assert event.extra["actual"] == actual


def test_a_platform_the_pin_does_not_cover_is_refused_rather_than_waved_through(
    tmp_path: Path,
) -> None:
    """A digest published beside another platform's asset describes the wrong bytes.

    The tempting repair — verify against whatever single digest the manifest
    happens to hold — passes on the one platform it was taken from and silently
    compares unrelated bytes on every other. So a host the pin does not name has
    no digest at all, and the verdict is a refusal, not a shrug.
    """
    sink = _RecordingSink()
    path = _write_artifact(tmp_path)
    module = _module()
    artifact = module.PinnedArtifact(
        pin=_pin(hashlib.sha256(path.read_bytes()).hexdigest(), platform="sunos/sparc"),
        path=path,
        installed_version="2.3.1",
    )

    with pytest.raises(ExtensionError):
        _verifier(sink).verify(artifact, caller_did=_CALLER)

    (event,) = _denials(sink)
    assert event.extra["reason"] == "unpinned_platform"
    assert host_platform() in event.extra["expected"]


# --- exact version -----------------------------------------------------------


def test_a_build_reporting_a_different_version_is_refused(tmp_path: Path) -> None:
    """Hash and version are separate clauses: matching one does not excuse the other."""
    artifact = _artifact(_write_artifact(tmp_path), version="2.3.1", installed="2.4.0")

    with pytest.raises(ExtensionError):
        _verifier(_RecordingSink()).verify(artifact, caller_did=_CALLER)


def test_the_version_refusal_names_both_versions(tmp_path: Path) -> None:
    sink = _RecordingSink()
    artifact = _artifact(_write_artifact(tmp_path), version="2.3.1", installed="2.4.0")

    with pytest.raises(ExtensionError):
        _verifier(sink).verify(artifact, caller_did=_CALLER)

    (event,) = _denials(sink)
    assert (event.extra["expected"], event.extra["actual"]) == ("2.3.1", "2.4.0")


# --- nothing is ever fetched -------------------------------------------------


def test_a_missing_artifact_is_refused_rather_than_obtained(tmp_path: Path) -> None:
    """The absent-artifact case is where a helpful implementation would reach out."""
    artifact = _artifact(tmp_path / "never-installed.tgz")

    with pytest.raises(ExtensionError):
        _verifier(_RecordingSink()).verify(artifact, caller_did=_CALLER)


def test_a_directory_cannot_stand_in_for_a_pinned_artifact(tmp_path: Path) -> None:
    """One digest pins one file; a tree whose contents can change under it is not pinned."""
    artifact = _artifact(tmp_path)

    with pytest.raises(ExtensionError):
        _verifier(_RecordingSink()).verify(artifact, caller_did=_CALLER)


def test_the_verifier_imports_nothing_that_could_fetch_or_execute() -> None:
    """Structural: a verifier able to download the expected bytes verifies nothing."""
    tree = ast.parse(inspect.getsource(_module()))
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert imported & _FETCH_CAPABLE == set()


# --- before EACH execution, enforced on the launch path ----------------------


async def test_a_tampered_artifact_never_reaches_exec(tmp_path: Path) -> None:
    """The verdict has to gate the spawn, not merely precede it in a docstring."""
    launcher_module = _launcher()
    sink = _RecordingSink()
    path = _write_artifact(tmp_path)
    artifact = _artifact(path)
    path.write_bytes(b"swapped")
    launcher = launcher_module.ProcessLauncher(
        policy=launcher_module.sandbox_policy_for(Tier.PERSONAL), verifier=_verifier(sink)
    )
    definition = launcher_module.ProcessDefinition(
        key="pinned",
        argv=[sys.executable, "-c", _STAY_ALIVE],
        artifact=artifact,
        owner_did=_CALLER,
    )

    try:
        with pytest.raises(ExtensionError):
            await launcher.acquire(definition)
        assert launcher.running == ()
    finally:
        await launcher.shutdown()


async def test_the_pin_is_rechecked_on_the_next_start_not_cached(tmp_path: Path) -> None:
    """Install-time-only verification passes the first start and misses the swap."""
    launcher_module = _launcher()
    sink = _RecordingSink()
    path = _write_artifact(tmp_path)
    artifact = _artifact(path)
    launcher = launcher_module.ProcessLauncher(
        policy=launcher_module.sandbox_policy_for(Tier.PERSONAL), verifier=_verifier(sink)
    )
    definition = launcher_module.ProcessDefinition(
        key="pinned",
        argv=[sys.executable, "-c", _STAY_ALIVE],
        artifact=artifact,
        owner_did=_CALLER,
        idle_timeout_seconds=0.0,
    )

    try:
        await launcher.acquire(definition)
        await launcher.reap_idle()
        path.write_bytes(b"swapped between runs")

        with pytest.raises(ExtensionError):
            await launcher.acquire(definition)
    finally:
        await launcher.shutdown()


async def test_a_pinned_artifact_will_not_start_without_a_verifier(tmp_path: Path) -> None:
    """Fail closed: an unconfigured verifier must refuse the run, never wave it through."""
    launcher_module = _launcher()
    launcher = launcher_module.ProcessLauncher(
        policy=launcher_module.sandbox_policy_for(Tier.PERSONAL)
    )
    definition = launcher_module.ProcessDefinition(
        key="pinned",
        argv=[sys.executable, "-c", _STAY_ALIVE],
        artifact=_artifact(_write_artifact(tmp_path)),
        owner_did=_CALLER,
    )

    try:
        with pytest.raises(ExtensionError):
            await launcher.acquire(definition)
        assert launcher.running == ()
    finally:
        await launcher.shutdown()
