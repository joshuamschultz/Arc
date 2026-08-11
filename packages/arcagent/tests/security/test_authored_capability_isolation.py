"""AA-020: authored capability code never executes in the ArcAgent host."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.isolated_tool import IsolatedCapabilityError
from arcagent.tools._dynamic_loader import resolve_workspace_import_policy


def _loader(path: Path, runner: Any) -> tuple[CapabilityLoader, CapabilityRegistry]:
    registry = CapabilityRegistry()
    policy = resolve_workspace_import_policy("personal", allow_all_imports=False, allow_imports=[])
    return (
        CapabilityLoader(
            scan_roots=[("workspace", path)],
            registry=registry,
            import_policy=policy,
            isolated_runner=runner,
        ),
        registry,
    )


@pytest.mark.asyncio
async def test_import_time_side_effect_does_not_run_in_host(tmp_path: Path) -> None:
    marker = tmp_path / "host-escaped"
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "safe.py").write_text(
        "from arcagent.tools._decorator import tool\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('escaped')\n"
        "@tool(description='safe')\n"
        "async def safe(value: str) -> str:\n"
        "    return value\n",
        encoding="utf-8",
    )

    async def runner(_source: str, request: dict[str, Any]) -> Any:
        return request["arguments"]["value"]

    loader, registry = _loader(caps, runner)
    delta = await loader.scan_and_register()
    assert delta.added == ["safe"]
    assert not marker.exists()
    entry = await registry.get_tool("safe")
    assert entry is not None
    assert await entry.execute(value="round-trip") == "round-trip"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_authored_lifecycle_is_rejected_not_instantiated(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "resident.py").write_text(
        "from arcagent.tools._decorator import capability\n"
        "@capability()\n"
        "class Resident:\n"
        "    pass\n",
        encoding="utf-8",
    )

    async def runner(_source: str, _request: dict[str, Any]) -> Any:
        raise AssertionError("refused capability must not reach runner")

    loader, _ = _loader(caps, runner)
    delta = await loader.scan_and_register()
    assert not delta.added
    assert "unsupported across the isolation boundary" in delta.errors[0][1]


@pytest.mark.asyncio
async def test_non_json_arguments_fail_before_ipc(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "safe.py").write_text(
        "from arcagent.tools._decorator import tool\n"
        "@tool(description='safe')\n"
        "async def safe(value: str) -> str:\n"
        "    return value\n",
        encoding="utf-8",
    )
    called = False

    async def runner(_source: str, _request: dict[str, Any]) -> Any:
        nonlocal called
        called = True

    loader, registry = _loader(caps, runner)
    await loader.scan_and_register()
    entry = await registry.get_tool("safe")
    assert entry is not None
    with pytest.raises(IsolatedCapabilityError, match="JSON serializable"):
        await entry.execute(value=object())
    assert not called


@pytest.mark.asyncio
async def test_a_module_declaring_no_tool_registers_nothing_and_is_not_an_error(
    tmp_path: Path,
) -> None:
    """Not-a-capability is not a broken capability.

    An extension bundle names exactly one attachment entrypoint in its manifest,
    so its other adapter variants and helper modules are ordinary Python with no
    ``@tool`` in them. Treating that as a refusal failed the whole install. The
    file must simply register nothing — and still never execute in the host.
    """
    marker = tmp_path / "host-escaped"
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "adapter.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('escaped')\n"
        "class HostedAttachment:\n"
        "    async def probe(self) -> str:\n"
        "        return 'reachable'\n",
        encoding="utf-8",
    )

    async def runner(_source: str, request: dict[str, Any]) -> Any:  # pragma: no cover
        raise AssertionError("nothing should be dispatched")

    loader, registry = _loader(caps, runner)
    delta = await loader.scan_and_register()

    assert delta.added == []
    assert delta.errors == []
    assert await registry.get_tool("HostedAttachment") is None
    # The whole point of the isolation boundary: still not imported in-host.
    assert not marker.exists()


@pytest.mark.asyncio
async def test_a_malformed_tool_still_fails_closed(tmp_path: Path) -> None:
    """Loosening the empty case must not loosen the broken case.

    A ``@tool`` on a sync function is a capability the author meant to ship and
    got wrong. That stays an error, or the previous test would have bought
    permissiveness rather than correctness.
    """
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "broken.py").write_text(
        "from arcagent.tools._decorator import tool\n"
        "@tool(description='sync is not allowed')\n"
        "def broken(value: str) -> str:\n"
        "    return value\n",
        encoding="utf-8",
    )

    async def runner(_source: str, request: dict[str, Any]) -> Any:  # pragma: no cover
        raise AssertionError("nothing should be dispatched")

    loader, _registry = _loader(caps, runner)
    delta = await loader.scan_and_register()

    assert delta.added == []
    assert [detail for _path, detail in delta.errors]
    assert "async function" in delta.errors[0][1]
