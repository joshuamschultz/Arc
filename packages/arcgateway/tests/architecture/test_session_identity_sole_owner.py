"""SessionIdentity is the sole owner of session-key derivation (SPEC-065 COMP-007, REQ-304).

Same shape as ``test_imports.py``: AST-based, parses source rather than
importing it, so a violation is a clean test failure and not a runtime
ImportError somewhere up the dependency chain.

What this guards
----------------
A session key is an *identity*. Every surface — arcui, arctui, arcgateway,
and every platform-adapter package — must obtain it from the one module that
owns the formula, never build one of its own. Two surfaces with two formulas
means the same (agent, user) pair gets two conversations, and a key built
outside the owner also skips the rotation generation the owner folds in, so
``/new`` silently fails to take effect on that surface.

The four rules below are deliberately narrow so the guard keeps biting:

    build     — a ``session_key``-named binding whose value is *constructed*
                (f-string, concatenation, ``.format()``/``.join()``, or a
                hashlib digest) instead of *obtained* from an owner call.
    hash      — a function that both reaches a digest primitive and names
                ``session_key`` anywhere in its own scope.
    rotate    — a non-owner passing ``generation=`` to ``build_session_key``,
                or touching ``SessionEpochStore``. Rotation belongs to the
                new-session command alone.
    redefine  — a non-owner defining ``build_session_key`` itself.

The vocabulary is ``session_key`` ONLY. ``chat_id`` is excluded on purpose:
adapters legitimately build a platform chat id out of platform parts, and
arcui's browser ``session_id`` is a random token, not a derived key — folding
either in would produce false positives and the guard would get relaxed to
silence them, which is how architecture tests rot into decoration.

``_derivation_violations`` is a pure function over source text so the positive
controls at the bottom can feed it known-bad snippets and prove it actually
fails. A guard that cannot fail is worse than no guard.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PACKAGES = _REPO_ROOT / "packages"

#: The ONE module that owns the derivation formula (COMP-007). It sits in
#: arctrust, the leaf every layer already depends on, because the agent side
#: needs the same key as the gateway side and arcagent may not import
#: arcgateway — an owner inside the gateway forces every layer below it to
#: compose its own. The anti-rot test below fails loudly if this constant and
#: the real formula ever part company, rather than passing over an empty owner.
_DERIVATION_OWNER = _PACKAGES / "arctrust" / "src" / "arctrust" / "session_identity.py"

#: Modules allowed to touch the rotation counter: the new-session command
#: (``SessionRouter``), the store it is implemented with, and the owner whose
#: signature exposes ``generation``. Nothing else may rotate.
_ROTATION_OWNERS = frozenset(
    {
        _DERIVATION_OWNER,
        _PACKAGES / "arcgateway" / "src" / "arcgateway" / "session.py",
        _PACKAGES / "arcgateway" / "src" / "arcgateway" / "session_epoch.py",
    }
)

#: Every surface that talks to an agent, plus the package holding the owner so
#: the scan judges the derivation itself and not only its consumers. A platform
#: adapter is a surface too — historically the most likely place to hand-roll a
#: key.
_SCANNED_PACKAGES = (
    "arctrust",
    # Since SPEC-065 the platform adapters live inside arcgateway, so scanning
    # that one package covers them. Listing the retired distributions here
    # would scan three directories that no longer exist and pass on nothing.
    "arcgateway",
    "arcui",
    "arctui",
)

#: Calls that legitimately yield a session key — the owner's public surface.
_OWNER_CALLS = frozenset({"build_session_key", "current_session_key", "new_session"})

_SESSION_KEY_NAME = re.compile(r"session_?key", re.IGNORECASE)

_DIGEST_CONSTRUCTORS = frozenset(
    {"sha1", "sha224", "sha256", "sha384", "sha512", "sha3_256", "md5", "blake2b", "blake2s"}
)


# ---------------------------------------------------------------------------
# Source scanning helpers
# ---------------------------------------------------------------------------


def _source_roots() -> list[Path]:
    """Every ``src/<pkg>/`` directory belonging to a scanned package."""
    roots: list[Path] = []
    for pkg in _SCANNED_PACKAGES:
        src = _PACKAGES / pkg / "src"
        if not src.is_dir():
            continue
        roots.extend(child for child in src.iterdir() if child.is_dir())
    return roots


def _python_files(root: Path) -> list[Path]:
    """Real source files under ``root`` — no caches, no vendored test trees."""
    return [
        p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts and "tests" not in p.parts
    ]


def _call_name(node: ast.Call) -> str | None:
    """Terminal name of a call: ``a.b.build_session_key(...)`` -> ``build_session_key``."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _uses_digest(node: ast.AST) -> bool:
    """True when the subtree reaches a hashing primitive.

    Covers ``hashlib.sha256(...)``, a bare ``sha256(...)`` from
    ``from hashlib import sha256``, and any ``.hexdigest()`` / ``.digest()``.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            if isinstance(child.value, ast.Name) and child.value.id == "hashlib":
                return True
        if isinstance(child, ast.Call):
            name = _call_name(child)
            if name in _DIGEST_CONSTRUCTORS or name in {"hexdigest", "digest"}:
                return True
    return False


def _construction_kind(node: ast.expr) -> str | None:
    """Describe how ``node`` builds a value, or None if it merely obtains one.

    Obtaining = a call to an owner function, or passing an existing name /
    attribute / subscript through. Building = composing the string here.
    """
    if isinstance(node, ast.JoinedStr):
        return "an f-string"
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            return "string concatenation"
        if isinstance(node.op, ast.Mod):
            return "%-formatting"
    if isinstance(node, (ast.IfExp,)):
        return _construction_kind(node.body) or _construction_kind(node.orelse)
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            kind = _construction_kind(value)
            if kind is not None:
                return kind
        return None
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in _OWNER_CALLS:
            return None
        if _uses_digest(node):
            return "a hashlib digest"
        if name in {"format", "join"}:
            return f".{name}()"
    return None


def _bound_names(target: ast.expr) -> list[str]:
    """Names an assignment target actually BINDS.

    A subscript index is not a binding: ``in_flight[session_key] = n + 1`` stores
    a counter under an existing key, it does not build a session key. Walking the
    whole target instead would read that index as a binding and flag ordinary
    bookkeeping as a rogue derivation.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for elt in target.elts for name in _bound_names(elt)]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    return []


def _scope_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names the function itself introduces: its own name, args, and bindings."""
    names = {func.name}
    args = func.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        names.add(arg.arg)
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_bound_names(target))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _is_session_key_name(name: str) -> bool:
    return bool(_SESSION_KEY_NAME.search(name))


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


def _derivation_violations(
    source: str,
    label: str,
    *,
    is_derivation_owner: bool = False,
    is_rotation_owner: bool = False,
) -> list[str]:
    """Return one message per session-identity violation found in ``source``.

    Pure over text so the positive controls below can prove it bites.
    """
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError:  # a file that cannot parse cannot be judged
        return []

    found: list[str] = []

    def report(node: ast.AST, detail: str) -> None:
        found.append(f"{label}:{getattr(node, 'lineno', 0)}: {detail}")

    # ── build: a session_key-named binding whose value is constructed here ──
    if not is_derivation_owner:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in _bound_names(target):
                        if _is_session_key_name(name):
                            kind = _construction_kind(node.value)
                            if kind:
                                report(node, f"builds {name} from {kind}")
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name) and _is_session_key_name(node.target.id):
                    kind = _construction_kind(node.value)
                    if kind:
                        report(node, f"builds {node.target.id} from {kind}")
            elif isinstance(node, ast.keyword):
                if node.arg and _is_session_key_name(node.arg):
                    kind = _construction_kind(node.value)
                    if kind:
                        report(node.value, f"passes {node.arg}= built from {kind}")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _is_session_key_name(node.name):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value is not None:
                    kind = _construction_kind(child.value)
                    if kind:
                        report(child, f"{node.name}() returns a key built from {kind}")

    # ── hash: a function that hashes AND names session_key in its own scope ──
    if not is_derivation_owner:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _uses_digest(node):
                continue
            if any(_is_session_key_name(n) for n in _scope_names(node)):
                report(node, f"{node.name}() hashes its own session key")

    # ── rotate: generation belongs to the new-session command alone ──────────
    if not is_rotation_owner:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "build_session_key":
                for kw in node.keywords:
                    if kw.arg == "generation":
                        report(node, "passes its own generation= to build_session_key")
            if isinstance(node, ast.Name) and node.id == "SessionEpochStore":
                report(node, "touches SessionEpochStore (rotation is not this module's)")
            if isinstance(node, ast.Attribute) and node.attr == "SessionEpochStore":
                report(node, "touches SessionEpochStore (rotation is not this module's)")

    # ── redefine: only the owner may define the formula ──────────────────────
    if not is_derivation_owner:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "build_session_key":
                    report(node, "redefines build_session_key")

    return sorted(set(found))


# ---------------------------------------------------------------------------
# Positive controls — the detector must FAIL on known-bad code
# ---------------------------------------------------------------------------

_BAD_HASHLIB_OWN_FORMULA = """
import hashlib

def build_session_key(agent_did: str, user_did: str) -> str:
    return hashlib.sha256(f"{agent_did}:{user_did}".encode()).hexdigest()[:16]
"""

_BAD_LOCAL_HASH = """
import hashlib

def _route(agent_did, user_did):
    session_key = hashlib.sha256((agent_did + user_did).encode()).hexdigest()[:16]
    return session_key
"""

_BAD_FSTRING_KEYWORD = """
def on_message(self, channel, user_id):
    return InboundEvent(
        platform="slack",
        session_key=f"slack:{channel}:{user_id}",
    )
"""

_BAD_OWN_GENERATION = """
from arcgateway.session import build_session_key

def refresh(agent_did, user_did, n):
    return build_session_key(agent_did, user_did, generation=n + 1)
"""

_BAD_EPOCH_STORE = """
from arcgateway.session_epoch import SessionEpochStore

def rotate(base):
    return SessionEpochStore().bump(base)
"""

_GOOD_OWNER_CALL = """
def connect(router, agent_did, user_did):
    session_key = router.current_session_key(agent_did, user_did)
    return session_key
"""

_GOOD_PASSTHROUGH = """
def cancel(body):
    return Cancellation(session_key=str(body.get("session_key") or ""))
"""

_GOOD_UNRELATED_HASH = """
import hashlib

def derive_viewer_did(viewer_token: str) -> str:
    return "did:arc:viewer:" + hashlib.sha256(viewer_token.encode("utf-8")).hexdigest()[:16]
"""

_GOOD_PLATFORM_CHAT_ID = """
def target_for(platform, chat):
    chat_id = f"{platform}:{chat}"
    return chat_id
"""

_GOOD_KEYED_BOOKKEEPING = """
def count(in_flight, session_key):
    in_flight[session_key] = in_flight.get(session_key, 0) + 1
    return in_flight
"""


@pytest.mark.parametrize(
    ("snippet", "expected_fragment"),
    [
        (_BAD_HASHLIB_OWN_FORMULA, "redefines build_session_key"),
        (_BAD_LOCAL_HASH, "hashes its own session key"),
        (_BAD_FSTRING_KEYWORD, "session_key= built from an f-string"),
        (_BAD_OWN_GENERATION, "own generation="),
        (_BAD_EPOCH_STORE, "SessionEpochStore"),
    ],
    ids=["own-formula", "local-hash", "f-string-key", "own-generation", "epoch-store"],
)
def test_detector_flags_known_bad_code(snippet: str, expected_fragment: str) -> None:
    """Positive control: the guard must bite on a rogue derivation.

    Without this, a scan that silently matches nothing would report GREEN
    forever and the invariant would be unprotected.
    """
    violations = _derivation_violations(snippet, "<rogue>")
    assert violations, f"detector missed a rogue derivation:\n{snippet}"
    assert any(expected_fragment in v for v in violations), (
        f"detector flagged the wrong thing — wanted {expected_fragment!r}, got {violations}"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        _GOOD_OWNER_CALL,
        _GOOD_PASSTHROUGH,
        _GOOD_UNRELATED_HASH,
        _GOOD_PLATFORM_CHAT_ID,
        _GOOD_KEYED_BOOKKEEPING,
    ],
    ids=["owner-call", "passthrough", "viewer-did", "platform-chat-id", "keyed-bookkeeping"],
)
def test_detector_allows_legitimate_code(snippet: str) -> None:
    """Negative control: obtaining a key, or hashing something else, is fine.

    A guard that flags everything gets deleted just as fast as one that flags
    nothing.
    """
    assert _derivation_violations(snippet, "<ok>") == []


# ---------------------------------------------------------------------------
# The real guard
# ---------------------------------------------------------------------------


def test_owner_module_actually_owns_the_derivation() -> None:
    """Anti-rot: the named owner must really contain the formula.

    If ``build_session_key`` moves and this constant is not updated, every
    scan below would happily pass while judging a module that owns nothing.
    This test makes that failure loud instead of silent.
    """
    assert _DERIVATION_OWNER.exists(), f"session-identity owner missing: {_DERIVATION_OWNER}"
    tree = ast.parse(_DERIVATION_OWNER.read_text(encoding="utf-8"))
    owner_fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "build_session_key"
        ),
        None,
    )
    assert owner_fn is not None, (
        f"{_DERIVATION_OWNER} no longer defines build_session_key — the session-identity "
        "owner moved. Update _DERIVATION_OWNER to the new module (COMP-007)."
    )
    assert _uses_digest(owner_fn), (
        "build_session_key no longer hashes — the derivation formula moved out of the "
        "owner module and this guard is now pointing at a shell."
    )


def test_scan_covers_every_surface_package() -> None:
    """Anti-rot: the scan must actually reach files.

    A typo in a package name would shrink the corpus to nothing and turn the
    guard below into a no-op that always passes.
    """
    roots = _source_roots()
    assert roots, "no surface source roots found — the scan would be vacuous"
    scanned = {p for root in roots for p in _python_files(root)}
    assert len(scanned) > 50, f"suspiciously small scan corpus: {len(scanned)} files"
    assert _DERIVATION_OWNER in scanned, "the owner module itself is outside the scan"


def test_no_surface_derives_its_own_session_key() -> None:
    """No surface but the owner may build, hash, or rotate a session key.

    REQ-304 / COMP-007: "Sole owner of how a session key is derived and
    rotated, for every surface. Rotation only on the explicit new-session
    command." A key composed anywhere else is a second identity for the same
    (agent, user) pair, and it silently skips the rotation generation, so
    ``/new`` does not take effect on that surface.
    """
    violations: list[str] = []
    for root in _source_roots():
        for path in _python_files(root):
            violations.extend(
                _derivation_violations(
                    path.read_text(encoding="utf-8"),
                    str(path.relative_to(_REPO_ROOT)),
                    is_derivation_owner=path == _DERIVATION_OWNER,
                    is_rotation_owner=path in _ROTATION_OWNERS,
                )
            )

    assert not violations, (
        "a surface derives its own session key instead of asking the owner "
        f"({_DERIVATION_OWNER.relative_to(_REPO_ROOT)}).\n"
        "Call SessionRouter.current_session_key(agent_did, user_did) for the current key, "
        "or SessionRouter.new_session(...) to rotate. Never compose one locally — a "
        "hand-built key is platform-scoped and skips the rotation generation.\n\n"
        + "\n".join(f"  {v}" for v in violations)
    )
