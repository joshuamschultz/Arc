"""Architecture test: ONE resolver for the operator key (alpha-2 item 70).

``arctrust.operator_public_key_for`` / ``operator_signer_for`` answer "which
operator key?" under every custody (key file in_process, transit handle
vault_transit). A package that calls ``OperatorKey.load(`` itself reads the key
FILE and so disagrees with the vault under federal custody: workflows, skills
and schedules signed by the vault key fail to verify against a file that does
not exist, or worse against a stale one.

Allowed: ``arctrust`` (the resolver) and ``arccli`` (the operator commands that
mint and rotate the key). Every other call site must go through the resolver.

The few sites that need the raw SEED (message-envelope signing, detached
artifact signing, the runner identity) cannot be handed a vault handle yet;
they are listed in ``_SEED_HOLDERS`` with the reason, so the list is a visible
debt and any NEW site fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PACKAGES = _REPO / "packages"
_RESOLVER_PACKAGES = {"arctrust", "arccli"}

#: path (relative to ``packages/``) -> why it still holds the raw key.
_SEED_HOLDERS = {
    "arcagent/src/arcagent/core/agent_security.py": (
        "agent startup: in_process load with vault_resolver + bootstrap + prior-chain guard"
    ),
    "arcteam/src/arcteam/workflow/identity.py": "runner narration signs envelopes with the seed",
    "arcui/src/arcui/prompt_signing.py": "detached artifact signature takes a private key",
    "arcui/src/arcui/messaging.py": "operator message signer takes the Ed25519 seed",
}


def _calls_operator_key_load(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "load"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "OperatorKey"
    ]


def find_violations(packages: Path) -> dict[str, list[int]]:
    """Every ``OperatorKey.load(`` in package source outside the resolver packages."""
    found: dict[str, list[int]] = {}
    for path in packages.glob("*/src/**/*.py"):
        rel = path.relative_to(packages)
        if rel.parts[0] in _RESOLVER_PACKAGES:
            continue
        lines = _calls_operator_key_load(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            found[rel.as_posix()] = lines
    return found


def test_no_operator_key_load_outside_arctrust_and_arccli() -> None:
    unexpected = {k: v for k, v in find_violations(_PACKAGES).items() if k not in _SEED_HOLDERS}
    assert not unexpected, (
        "OperatorKey.load( outside arctrust/arccli reads the key FILE and breaks "
        "vault-held (federal) custody; use arctrust.operator_public_key_for / "
        f"operator_signer_for: {unexpected}"
    )


def test_seed_holder_list_has_no_stale_entries() -> None:
    stale = set(_SEED_HOLDERS) - set(find_violations(_PACKAGES))
    assert not stale, f"remove from _SEED_HOLDERS, no longer loads the key: {sorted(stale)}"


def test_a_planted_operator_key_load_is_caught(tmp_path: Path) -> None:
    planted = tmp_path / "arcfoo" / "src" / "arcfoo" / "bad.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "from arctrust import OperatorKey\n"
        "def f(p):\n"
        "    return OperatorKey.load(p, generate_if_absent=False).public_key\n"
    )
    assert find_violations(tmp_path) == {"arcfoo/src/arcfoo/bad.py": [3]}
