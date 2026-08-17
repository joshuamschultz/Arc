"""ArcAgent must not *invoke* a model, not merely must not import one (ADR-032).

The existing boundary guards read import statements. A bypass that travels
through an object handle writes no import at all, so ``ArcAgent.quick_classify``
held an ArcLLM provider and called ``model.invoke()`` on it while twelve
thousand tests stayed green. Import direction was never the invariant; who owns
the model call was.

The invariant this file enforces: **ArcAgent may hold a model handle and hand it
to ArcRun, and may never call a provider method on it.** Every model call in the
stack therefore has one owner, and cheap inference goes through
``arcrun.run_oneshot`` rather than around it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ARCAGENT_SRC = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent"

# The ArcLLM provider protocol. Calling any of these on a handle is ArcAgent
# doing ArcRun's job.
_PROVIDER_METHODS = frozenset({"invoke", "invoke_stream", "embed", "embed_stream"})

# ``invoke`` is not a reserved word, and ArcAgent owns protocols that use it for
# something that is not a model: an extension attachment dispatches a *tool* by
# ``invoke(tool, args)``, and ``skills.LLMInvoker`` takes a prompt and returns
# text. Those receivers are named here, so the scan stays a guard rather than a
# nuisance and so adding one is a visible decision rather than a quiet one. A
# name earns a place here only when what it holds is provably not a provider.
_NON_PROVIDER_RECEIVERS = frozenset({"attachment", "_attachment", "_delegate", "_invoker"})

# One provider-handle call site remains, pinned rather than hidden.
# ``modules/browser/_browser_use/adapter.py`` is a shim that implements the
# *browser-use* library's chat interface on top of an Arc model. Routing it
# through ``run_oneshot`` would mean widening ``LoopResult`` to carry provider
# response internals (cache-token counts, stop reason, raw tool calls) so one
# optional third-party adapter could read them back out — which would weaken the
# very boundary this test defends. Pinned as an exact set: this file passes only
# while the debt is exactly this and no larger.
_PINNED_DEBT = frozenset(
    {
        "modules/browser/_browser_use/adapter.py:invoke",
    }
)


@dataclass(frozen=True)
class _ProviderCall:
    path: Path
    line: int
    receiver: str
    method: str

    def key(self) -> str:
        return f"{self.path.relative_to(_ARCAGENT_SRC).as_posix()}:{self.method}"

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.receiver}.{self.method}(...)"


def _receiver_name(node: ast.expr) -> str:
    """The simple name a call was made on, or '' when it is an expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def provider_handle_calls(paths: Iterable[Path]) -> list[_ProviderCall]:
    """Every call of an ArcLLM provider method on a handle, in *paths*."""
    found: list[_ProviderCall] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _PROVIDER_METHODS:
                continue
            receiver = _receiver_name(node.func.value)
            if receiver in _NON_PROVIDER_RECEIVERS:
                continue
            found.append(_ProviderCall(path, node.lineno, receiver, node.func.attr))
    return found


def test_the_scanner_sees_a_handle_call_that_no_import_would_reveal() -> None:
    """Prove the guard can fail — on the exact shape that slipped through.

    The planted source imports nothing forbidden. Every import-based guard in the
    repo passes it. This one must not.
    """
    planted = _REPO_ROOT / "packages" / "arcagent" / "src" / "arcagent" / "__init__.py"
    tree = ast.parse(
        "import arcrun\n"
        "async def quick_classify(self, system, user):\n"
        "    model = self._ensure_model()\n"
        "    return await model.invoke([arcrun.Message(role='system', content=system)])\n"
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PROVIDER_METHODS
    ]

    assert planted.exists()
    assert len(calls) == 1, "the scanner must recognise a provider reached by handle"


def test_arcagent_never_calls_a_model_it_is_only_allowed_to_pass_on() -> None:
    sources = sorted(_ARCAGENT_SRC.rglob("*.py"))
    assert len(sources) > 250, "ArcAgent source discovery collapsed"

    calls = provider_handle_calls(sources)
    unpinned = [call for call in calls if call.key() not in _PINNED_DEBT]

    assert not unpinned, (
        "ArcAgent invoked a model directly. Cheap inference belongs in "
        "`arcrun.run_oneshot`; a full turn belongs in `arcrun.run_stream`:\n"
        + "\n".join(str(call) for call in unpinned)
    )


def test_the_pinned_debt_has_not_grown_and_is_still_real() -> None:
    """The ratchet. Debt may shrink without editing this file; it may not grow.

    An exact-set assertion is what makes the pin honest: a second adapter cannot
    quietly join the first, and removing the last one fails here loudly enough to
    delete the pin.
    """
    found = {call.key() for call in provider_handle_calls(sorted(_ARCAGENT_SRC.rglob("*.py")))}

    assert found & _PINNED_DEBT == found, (
        f"undeclared provider-handle debt: {found - _PINNED_DEBT}"
    )
    assert found == _PINNED_DEBT, f"pinned debt is stale; these are fixed: {_PINNED_DEBT - found}"
