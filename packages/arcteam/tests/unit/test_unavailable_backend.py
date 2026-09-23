"""Configured fleet storage refuses work while the shared bus is unavailable."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from arcteam import (
    FleetBackendUnavailableError,
    StorageBackend,
    UnavailableBackend,
    is_fleet_transport_error,
)
from arcteam.backends.nats import NatsBackend


async def test_unavailable_backend_fails_closed_for_reads_writes_and_consumers() -> None:
    backend = UnavailableBackend()
    assert isinstance(backend, StorageBackend)
    with pytest.raises(FleetBackendUnavailableError):
        await backend.read("entities", "agent://me")
    with pytest.raises(FleetBackendUnavailableError):
        await backend.append("messages", "agent://me", {"body": "not accepted"})
    with pytest.raises(FleetBackendUnavailableError):
        await backend.open_consumer("messages", "agent://me", "me")


def test_nats_availability_tracks_client_connection() -> None:
    client = SimpleNamespace(is_connected=True)
    backend = NatsBackend(js=object(), nc=client)
    assert backend.available
    client.is_connected = False
    assert not backend.available


def test_transport_classifier_never_hides_authorization_failure() -> None:
    assert is_fleet_transport_error(TimeoutError())
    assert is_fleet_transport_error(ConnectionRefusedError())
    assert not is_fleet_transport_error(PermissionError("denied"))


def test_standalone_import_and_backend_work_without_nats_client() -> None:
    script = """
import asyncio
import builtins
original_import = builtins.__import__
def without_nats(name, *args, **kwargs):
    if name == 'nats' or name.startswith('nats.'):
        raise ModuleNotFoundError("No module named 'nats'", name='nats')
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_nats
import arcteam
from arcteam import composition
assert isinstance(asyncio.run(composition.make_backend('')), arcteam.MemoryBackend)
try:
    asyncio.run(composition.make_backend('nats://127.0.0.1:4222'))
except arcteam.FleetBackendUnavailableError:
    pass
else:
    raise AssertionError('configured NATS without client was accepted')
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
