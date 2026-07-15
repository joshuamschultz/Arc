"""SPEC-056 SEC-F1 — the operator ``Signer`` is delivered by signature.

``configure_module_runtimes`` dispatches ``_runtime.configure(...)`` by
signature: a module receives ``operator_signer`` iff its ``configure()``
declares that parameter. Core names no module. The WORM audit-sink modules
(skills, messaging, planning, tasks) each declare the parameter, so they still
receive the deployment operator authority; a generic module that does not
declare it cannot harvest signing authority.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

_WORM_SINK_MODULES = ("skills", "messaging", "planning", "tasks")
_NON_SIGNER_MODULES = ("memory", "session", "proactive", "pulse")


def _configure_params(module_name: str) -> set[str]:
    runtime = importlib.import_module(f"arcagent.modules.{module_name}._runtime")
    return set(inspect.signature(runtime.configure).parameters)


@pytest.mark.parametrize("module_name", _WORM_SINK_MODULES)
def test_worm_sink_modules_declare_operator_signer(module_name: str) -> None:
    assert "operator_signer" in _configure_params(module_name)


@pytest.mark.parametrize("module_name", _NON_SIGNER_MODULES)
def test_non_signer_modules_do_not_declare_operator_signer(module_name: str) -> None:
    assert "operator_signer" not in _configure_params(module_name)


def test_core_names_no_module_for_signer_delivery() -> None:
    # The hardcoded module-name allowlist is gone: delivery is signature-driven.
    lifecycle = importlib.import_module("arcagent.core.agent_lifecycle")
    assert not hasattr(lifecycle, "_WORM_SINK_MODULE_NAMES")
