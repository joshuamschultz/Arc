"""T-939 — an adapter implements only lifecycle, payload-to-parts and send-parts.

SPEC-065 REQ-310, COMP-004. The requirement is a *negative* one and that is the
whole point of it: "the gateway SHALL retain download, naming, size ceilings,
audit, session identity, pairing and message splitting". Four adapters each
doing their own download and their own cap is four chances to forget the cap,
which is exactly the shape the SDD rejected.

A negative requirement cannot be tested by calling the good path — an adapter
that quietly re-implements the size ceiling passes every functional test in the
suite. So this file reads the adapter sources for the responsibilities that are
not theirs. That is not a style check: each marker below is one of the seven
properties REQ-310 names, and a hit means that property now has two
implementations that can disagree.

Precedent for the violation this catches: three adapters already hand-built
platform-scoped session keys (SPEC-065 Phase 1 learnings). They were inert only
because ``SessionRouter.handle`` overwrote them — one refactor away from a live
routing defect that no test would have seen.

Assumed of the T-937/T-939 implementation:

    registry.AdapterSpec(name, requires, supports, build)
    registry.discover_adapters() -> list[AdapterSpec]
    each platform lives at arcgateway.adapters.<name>
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

import pytest

from arcgateway.adapters import registry

#: The gateway's, not the adapter's. Each entry is (marker, which REQ-310
#: property it duplicates). Patterns are deliberately narrow so a mention in a
#: docstring does not trip them.
_NOT_THE_ADAPTERS_JOB: tuple[tuple[str, str], ...] = (
    (r"\bopen\s*\([^)]*[\"'][xwa]b[\"']", "download/storage — the gateway writes artefacts"),
    (r"\.write_bytes\s*\(", "download/storage — the gateway writes artefacts"),
    (r"\bMediaStore\b", "storage — the adapter must not drive the store itself"),
    (r"[\"']inbox[\"']", "naming — the gateway composes the workspace path"),
    (r"\bmax_bytes\b|\bMAX_BYTES\b", "size ceiling — one ceiling, in the gateway"),
    (r"\bbuild_session_key\b", "session identity — SessionIdentity owns the key"),
    (r"\bsession_key\s*=", "session identity — adapters supply platform identity only"),
    (r"\bPairingStore\b|\bpairing_store\b", "pairing — the gateway is the boundary"),
    (r"\bdef split_message\b", "splitting — the gateway splits at platform limits"),
)

#: The whole surface COMP-004 grants an adapter.
_LIFECYCLE = ("connect", "disconnect")
_TRANSLATION = ("on_payload", "to_parts")  # either name satisfies payload -> parts
_DELIVERY = ("send",)


#: Platforms REQ-308 requires, used as placeholders when the scan does not
#: exist yet. Collection must not error: a collection error takes the whole
#: file with it, and a skipped or uncollected test reads as "no failure".
_REQUIRED_PLATFORMS = ("telegram", "slack", "mattermost")


@dataclass(frozen=True)
class _Undiscovered:
    """Stand-in for a platform the scan could not offer."""

    name: str


def _specs() -> list[Any]:
    discover = getattr(registry, "discover_adapters", None)
    if discover is None:
        return [_Undiscovered(name) for name in _REQUIRED_PLATFORMS]
    return list(discover())


def _spec_ids(specs: Iterable[Any]) -> list[str]:
    return [str(getattr(spec, "name", spec)) for spec in specs]


def _platform_sources(name: str) -> list[Path]:
    """Every .py file belonging to one discovered platform."""
    module = importlib.import_module(f"arcgateway.adapters.{name}")
    paths = getattr(module, "__path__", None)
    if paths:
        return sorted(Path(paths[0]).rglob("*.py"))
    return [Path(module.__file__ or "")]


_SPECS = _specs()


# --- The declaration itself --------------------------------------------------


def test_adapter_spec_declares_requirements_and_capabilities() -> None:
    """COMP-004: AdapterSpec(name, requires, supports, build)."""
    spec_type = getattr(registry, "AdapterSpec", None)
    assert spec_type is not None, "registry exports no AdapterSpec"
    assert is_dataclass(spec_type), "AdapterSpec is not a dataclass"

    declared = {field.name for field in fields(spec_type)}
    expected = {"name", "requires", "supports", "build"}
    missing = expected - declared
    assert not missing, f"AdapterSpec is missing {sorted(missing)} (has {sorted(declared)})"


@pytest.mark.parametrize("spec", _SPECS, ids=_spec_ids(_SPECS))
def test_every_platform_declares_its_optional_capabilities(spec: Any) -> None:
    """``supports`` is an explicit declaration the gateway can degrade against.

    Undeclared is not the same as unsupported: the gateway has to know what a
    platform cannot carry *before* it tries, or degradation is an exception
    handler and the turn is already at risk.
    """
    supports = getattr(spec, "supports", None)
    assert supports is not None, f"{spec.name}: declares no capability set"
    assert not isinstance(supports, str), (
        f"{spec.name}: supports is a bare string, not a collection of capabilities"
    )
    assert all(isinstance(item, str) for item in supports), (
        f"{spec.name}: capability names must be strings, got {supports!r}"
    )


# --- The surface -------------------------------------------------------------


@pytest.mark.parametrize("spec", _SPECS, ids=_spec_ids(_SPECS))
def test_the_adapter_surface_is_lifecycle_translation_and_send(spec: Any) -> None:
    """Three responsibilities, and the adapter answers for all three."""
    module = importlib.import_module(f"arcgateway.adapters.{spec.name}")
    surface = _adapter_class(module)
    assert surface is not None, f"{spec.name}: no adapter class found in its module"

    for method in _LIFECYCLE + _DELIVERY:
        assert callable(getattr(surface, method, None)), (
            f"{spec.name}: missing {method}() — lifecycle and send are the adapter's"
        )

    assert any(callable(getattr(surface, name, None)) for name in _TRANSLATION), (
        f"{spec.name}: no payload-to-parts translation ({' / '.join(_TRANSLATION)}) — "
        "without it the adapter is still deciding what an inbound message is"
    )


@pytest.mark.parametrize("spec", _SPECS, ids=_spec_ids(_SPECS))
def test_the_adapter_does_not_re_implement_a_gateway_responsibility(spec: Any) -> None:
    """REQ-310's seven properties have exactly one implementation each."""
    violations: list[str] = []
    for source in _platform_sources(spec.name):
        text = source.read_text(encoding="utf-8")
        for pattern, why in _NOT_THE_ADAPTERS_JOB:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                violations.append(f"{source.name}:{line}: {match.group(0)!r} — {why}")

    assert not violations, (
        f"{spec.name}: adapter re-implements gateway responsibilities:\n"
        + "\n".join(violations)
    )


def _adapter_class(module: Any) -> Any:
    """The adapter class a platform module exposes, by any plausible name."""
    for attribute in vars(module).values():
        if isinstance(attribute, type) and attribute.__name__.endswith("Adapter"):
            return attribute
    return None
