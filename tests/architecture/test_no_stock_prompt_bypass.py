"""SPEC-083 T-1226 (REQ-513, COMP-030) — no package bypasses the prompt seam.

Three structural invariants behind "an ArcUI prompt edit reaches the model":

1. **One stock reader.** Production code calls ``arcprompt.load_stock`` /
   ``load_stock_document`` only inside arcprompt's ``StockPromptSource`` (the default
   ``PromptSource``). A consumer that calls ``load_stock`` directly reads the system
   layer and silently skips the agent's override — the defect this spec closes.
   Operator tooling that must show the *stock layer as such* (to diff an override
   against it, or to render a new override from it) is listed in
   ``_STOCK_LAYER_VIEWERS`` with its reason; none of it sends text to a model.

2. **Layering.** ``arcrun``, ``arcmemory`` and ``arcllm`` sit below the agent. They
   accept a ``PromptSource`` from arcprompt and never import arcagent, and never
   build the overlay-aware, agent-rooted ``PromptResolver`` themselves (that needs
   an agent folder and the operator key — agent knowledge).

3. **No inline prompts.** Every model-bound instruction text ships as
   ``<package>/context/<name>.md``. The inline-prompt heuristic below flags prose
   string literals sent to a model from Python.

The inline-prompt heuristic, precisely
--------------------------------------
A *prompt sink* is one of:

* a keyword argument named ``system``, ``system_prompt``, ``instructions`` or
  ``criteria`` on any call (``run_oneshot(system=...)``, ``ChoiceSpec(instructions=...)``);
* every argument of a call to ``ClassificationRequest``, ``ChoiceSpec`` or ``NoulSpec``
  (arcllm's classifier question types);
* the ``content`` of a ``Message(role="system", ...)`` call, the first argument of
  ``system_message(...)``, and the ``"content"`` value of a ``{"role": "system", ...}``
  dict literal.

The value at a sink is reduced to its literal prose: a ``str`` constant, the
literal parts of an f-string, a ``+`` concatenation, and container literals
(dict/list/tuple) recursively. A name — or a subscript/attribute rooted at one
(``PROMOTION_QUESTION["scope"]["instructions"]``) — is resolved and reduced the same way.
Names are followed, at most ``_MAX_HOPS`` times per sink, through exactly these
edges and nothing else: a function-local assignment, a module-level constant, a
``from x import NAME`` constant in another in-repo module, a ``{value}`` inside an
f-string, and a call to a same-module function or ``self`` method (its return
expressions and arguments). A finding is one sink site whose reachable literals
include a single literal of at least ``_MIN_PROSE_CHARS`` characters that contains a
space (prose — not an identifier, enum value or format key). Short labels, keys and
field names never trip it. The extra sinks ``sections[...] = value`` (the system-prompt
section mapping an agent assembles) and ``<x>.invoke(prompt)`` with one positional
argument (the prompt-in/text-out ``LLMInvoker`` seam) cover prompt text that is sent
as a system section or a single-shot prompt rather than a ``system=`` argument.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

_N = TypeVar("_N", bound=ast.AST)

REPO = Path(__file__).resolve().parents[2]
PACKAGES = REPO / "packages"

_STOCK_READERS = frozenset({"load_stock", "load_stock_document"})
_STOCK_SOURCE_CLASS = "StockPromptSource"

#: Operator tooling that reads the stock layer to SHOW or AUTHOR against it, never to
#: send it to a model. Path (relative to packages/) -> reason.
_STOCK_LAYER_VIEWERS: dict[str, str] = {
    "arcui/src/arcui/routes/agent_detail/prompts.py": (
        "ArcUI prompt view: shows stock beside the override and diffs them"
    ),
    "arccli/src/arccli/commands/prompt.py": (
        "`arc prompt show/diff/edit`: prints stock and seeds an override from it"
    ),
    "arcagent/src/arcagent/blueprints_materialize.py": (
        "blueprint materializer: renders a signed override from the stock document"
    ),
}

_LOWER_LAYERS = ("arcrun", "arcmemory", "arcllm")
_AGENT_ONLY_PROMPT_NAMES = frozenset({"PromptResolver"})

_MIN_PROSE_CHARS = 40
_SINK_KEYWORDS = frozenset({"system", "system_prompt", "instructions", "criteria"})
_SINK_CALLS = frozenset({"ClassificationRequest", "ChoiceSpec", "NoulSpec"})


# ---------------------------------------------------------------------------
# Shared AST helpers
# ---------------------------------------------------------------------------


def _production_files(package: str | None = None) -> Iterator[Path]:
    roots = [PACKAGES / package / "src"] if package else sorted(PACKAGES.glob("*/src"))
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts or "/context/" in path.as_posix():
                continue
            yield path


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(path: Path) -> str:
    return path.relative_to(PACKAGES).as_posix()


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _with_parents(tree: ast.AST) -> ast.AST:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]
    return tree


def _enclosing(node: ast.AST, kind: type[_N]) -> _N | None:
    current: ast.AST | None = getattr(node, "_parent", None)
    while current is not None:
        if isinstance(current, kind):
            return current
        current = getattr(current, "_parent", None)
    return None


# ---------------------------------------------------------------------------
# 1. One stock reader
# ---------------------------------------------------------------------------


def _stock_reads(path: Path) -> list[str]:
    """``file:line`` for every call to a stock reader outside the allowed homes."""
    tree = _with_parents(_parse(path))
    rel = _rel(path)
    in_arcprompt = rel.startswith("arcprompt/")
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in _STOCK_READERS:
            continue
        cls = _enclosing(node, ast.ClassDef)
        fn = _enclosing(node, ast.FunctionDef)
        if in_arcprompt and isinstance(cls, ast.ClassDef) and cls.name == _STOCK_SOURCE_CLASS:
            continue
        # arcprompt's own ``load_stock`` is a thin body over ``load_stock_document``.
        if in_arcprompt and isinstance(fn, ast.FunctionDef) and fn.name in _STOCK_READERS:
            continue
        found.append(f"{rel}:{node.lineno} {_call_name(node)}()")
    return found


def test_stock_prompts_are_read_only_through_stock_prompt_source() -> None:
    """A consumer that reads stock directly never sees the agent's ArcUI override."""
    violations = [
        hit
        for path in _production_files()
        if _rel(path) not in _STOCK_LAYER_VIEWERS
        for hit in _stock_reads(path)
    ]
    assert not violations, (
        "stock prompts read outside arcprompt.StockPromptSource — these consumers skip "
        "the agent override layer (take a PromptSource instead):\n  " + "\n  ".join(violations)
    )


def test_stock_prompt_source_exists_in_arcprompt() -> None:
    """COMP-030: arcprompt owns the ``PromptSource`` contract and its stock default."""
    import arcprompt

    assert hasattr(arcprompt, "PromptSource"), "arcprompt exports no PromptSource Protocol"
    assert hasattr(arcprompt, _STOCK_SOURCE_CLASS), "arcprompt exports no StockPromptSource"
    source = getattr(arcprompt, _STOCK_SOURCE_CLASS)()
    assert source.resolve("arcagent", "base_system") == arcprompt.load_stock(
        "arcagent", "base_system"
    )


def test_stock_layer_viewers_still_exist() -> None:
    """An allowlisted viewer that moved or was deleted must leave the allowlist."""
    missing = sorted(rel for rel in _STOCK_LAYER_VIEWERS if not (PACKAGES / rel).is_file())
    assert not missing, f"stale stock-layer viewer entries: {missing}"


# ---------------------------------------------------------------------------
# 2. Layering
# ---------------------------------------------------------------------------


def _imports(path: Path) -> Iterator[tuple[int, str, list[str]]]:
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, []
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module, [a.name for a in node.names]


def test_lower_layers_never_import_arcagent() -> None:
    violations = [
        f"{_rel(path)}:{line} imports {module}"
        for package in _LOWER_LAYERS
        for path in _production_files(package)
        for line, module, _names in _imports(path)
        if module == "arcagent" or module.startswith("arcagent.")
    ]
    assert not violations, "a package below the agent imports arcagent:\n  " + "\n  ".join(
        violations
    )


def test_lower_layers_take_a_prompt_source_not_the_agent_resolver() -> None:
    """Below the agent, prompts come in as a PromptSource; the agent builds the resolver."""
    violations = [
        f"{_rel(path)}:{line} imports {', '.join(sorted(set(names) & _AGENT_ONLY_PROMPT_NAMES))}"
        for package in _LOWER_LAYERS
        for path in _production_files(package)
        for line, module, names in _imports(path)
        if module.startswith("arcprompt") and set(names) & _AGENT_ONLY_PROMPT_NAMES
    ]
    assert not violations, (
        "a package below the agent builds the overlay-aware PromptResolver itself:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# 3. No inline prompts (the heuristic in the module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InlinePrompt:
    where: str
    sink: str
    text: str

    def __str__(self) -> str:
        preview = self.text if len(self.text) <= 70 else self.text[:67] + "..."
        return f"{self.where} [{self.sink}] {preview!r}"


class _Module:
    """One parsed source file: its module constants, functions and imported names."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = _with_parents(tree)
        self.constants: dict[str, ast.expr] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.imported: dict[str, str] = {}  # local name -> "package.module"
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions.setdefault(node.name, node)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.constants[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    self.constants[node.target.id] = node.value
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    self.imported[alias.asname or alias.name] = node.module


_MODULE_CACHE: dict[str, _Module | None] = {}


def _module_named(dotted: str) -> _Module | None:
    """The in-repo source of ``dotted`` (``packages/*/src/<a>/<b>.py``), parsed once."""
    if dotted not in _MODULE_CACHE:
        rel = Path(*dotted.split("."))
        hits = [
            p
            for root in PACKAGES.glob("*/src")
            for p in (root / rel.with_suffix(".py"), root / rel / "__init__.py")
            if p.is_file()
        ]
        _MODULE_CACHE[dotted] = _Module(_parse(hits[0])) if hits else None
    return _MODULE_CACHE[dotted]


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Subscript | ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _local_value(name: str, at: ast.AST) -> ast.expr | None:
    """The value first assigned to ``name`` inside the function enclosing ``at``."""
    fn = _enclosing(at, ast.FunctionDef) or _enclosing(at, ast.AsyncFunctionDef)
    if fn is None:
        return None
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    return None


_MAX_HOPS = 4


def _literals(node: ast.expr, mod: _Module, hops: int = _MAX_HOPS) -> list[str]:
    """Literal prose reachable from ``node``, following at most ``_MAX_HOPS`` names/calls."""
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, ast.JoinedStr):
        literal = "".join(
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        formatted = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
        return [literal, *(s for f in formatted for s in _literals(f, mod, hops))]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literals(node.left, mod, hops) + _literals(node.right, mod, hops)
    if isinstance(node, ast.Dict):
        return [s for v in node.values if v is not None for s in _literals(v, mod, hops)]
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return [s for v in node.elts for s in _literals(v, mod, hops)]
    if hops == 0:
        return []
    if isinstance(node, ast.Call):
        return _call_literals(node, mod, hops)
    return _name_literals(node, mod, hops)


def _call_literals(node: ast.Call, mod: _Module, hops: int) -> list[str]:
    """Any call: its arguments. A same-module function/method: also its return values."""
    func = node.func
    local = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute) and _root_name(func) == "self"
        else None
    )
    found = [s for a in node.args for s in _literals(a, mod, hops - 1)]
    if local and local in mod.functions:
        returns = [r.value for r in ast.walk(mod.functions[local]) if isinstance(r, ast.Return)]
        found += [s for r in returns if r is not None for s in _literals(r, mod, hops - 1)]
    return found


def _name_literals(node: ast.expr, mod: _Module, hops: int) -> list[str]:
    """A name (or subscript/attribute of one): local value, module constant, or import."""
    resolved = _resolve(node, mod, hops)
    return _literals(resolved[0], resolved[1], hops - 1) if resolved else []


def _keys(node: ast.expr) -> tuple[ast.expr, list[str | None]]:
    """``a["x"].y["z"]`` -> (``a``, ["x", None, "z"]) — None where a key is not a literal."""
    keys: list[str | None] = []
    while isinstance(node, ast.Subscript | ast.Attribute):
        if isinstance(node, ast.Subscript):
            key = node.slice
            keys.append(
                key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
            )
        else:
            keys.append(None)
        node = node.value
    return node, keys[::-1]


def _navigate(value: ast.expr, keys: list[str | None]) -> ast.expr:
    """Follow literal keys into a dict literal (through a wrapping call); stop when unsure."""
    for key in keys:
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        if key is None or not isinstance(value, ast.Dict):
            return value
        match = [
            v
            for k, v in zip(value.keys, value.values, strict=True)
            if isinstance(k, ast.Constant) and k.value == key
        ]
        if not match:
            return value
        value = match[0]
    return value


def _resolve(node: ast.expr, mod: _Module, hops: int) -> tuple[ast.expr, _Module] | None:
    """The expression a (subscripted) name denotes, via local / module / imported value."""
    base, keys = _keys(node)
    if not isinstance(base, ast.Name) or hops <= 0:
        return None
    name = base.id
    value, owner = _local_value(name, node), mod
    if value is None and name in mod.constants:
        value = mod.constants[name]
    if value is None and name in mod.imported:
        other = _module_named(mod.imported[name])
        if other is not None and name in other.constants:
            value, owner = other.constants[name], other
    if value is None:
        return None
    if isinstance(value, ast.Name | ast.Subscript):
        deeper = _resolve(value, owner, hops - 1)
        if deeper is None:
            return None
        value, owner = deeper
    return _navigate(value, keys), owner


def _is_system_role(call: ast.Call) -> bool:
    return any(
        kw.arg == "role" and isinstance(kw.value, ast.Constant) and kw.value.value == "system"
        for kw in call.keywords
    )


def _sinks(tree: ast.AST) -> Iterator[tuple[ast.AST, str, ast.expr]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _root_name(target) == "sections":
                    yield node, "sections[...] =", node.value
        if isinstance(node, ast.Dict):
            pairs = {
                k.value: v
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            role = pairs.get("role")
            if isinstance(role, ast.Constant) and role.value == "system" and "content" in pairs:
                yield node, "system message dict", pairs["content"]
        if not isinstance(node, ast.Call):
            continue
        yield from _call_sinks(node)


def _call_sinks(node: ast.Call) -> Iterator[tuple[ast.AST, str, ast.expr]]:
    name = _call_name(node)
    if name in _SINK_CALLS:
        for arg in node.args:
            yield node, f"{name}()", arg
        for kw in node.keywords:
            yield node, f"{name}({kw.arg}=)", kw.value
        return
    for kw in node.keywords:
        if kw.arg in _SINK_KEYWORDS:
            yield node, f"{name}({kw.arg}=)", kw.value
        elif kw.arg == "content" and name == "Message" and _is_system_role(node):
            yield node, "Message(role='system')", kw.value
    if name == "system_message" and node.args:
        yield node, "system_message()", node.args[0]
    if name == "invoke" and len(node.args) == 1 and not node.keywords:
        yield node, "invoke(prompt)", node.args[0]


def _is_prose(text: str) -> bool:
    return len(text.strip()) >= _MIN_PROSE_CHARS and " " in text.strip()


def _findings(mod: _Module, label: str) -> list[InlinePrompt]:
    """One finding per sink site, previewing its longest literal prose."""
    found: list[InlinePrompt] = []
    for node, sink, value in _sinks(mod.tree):
        texts = [t.strip() for t in _literals(value, mod) if _is_prose(t)]
        if texts:
            where = f"{label}:{getattr(node, 'lineno', 0)}"
            found.append(InlinePrompt(where, sink, max(texts, key=len)))
    return list(dict.fromkeys(found))


def test_heuristic_flags_known_inline_shapes_and_ignores_loaded_prompts() -> None:
    """The heuristic's own proof: it sees each inline shape and nothing in a loaded prompt."""
    source = (
        "QUESTION = {'scope': {\n"
        "    'instructions': 'Who is this remembered fact or method useful to?'}}\n"
        "def a(m):\n"
        "    return run_oneshot(m, system='You are a careful assistant who answers briefly.')\n"
        "def b():\n"
        "    scope = QUESTION['scope']\n"
        "    return ChoiceSpec(instructions=scope['instructions'])\n"
        "def c(x):\n"
        "    return system_message(f'Select the best execution strategy for {x} today.')\n"
        "def d(m, resolve):\n"
        "    return run_oneshot(m, system=resolve('arcagent', 'planner_system'))\n"
        "def e():\n"
        "    return Message(role='user', content='This user text is long enough to matter.')\n"
        "def _judge(case):\n"
        "    return f'You are a strict evaluator. Apply the rubric: {case}'\n"
        "async def f(judge, case):\n"
        "    return await judge.invoke(_judge(case))\n"
        "def g(sections):\n"
        "    lines = ['You are an autonomous agent. Work silently and efficiently.']\n"
        "    sections['teams'] = '\\n'.join(lines)\n"
        "def h(sections, resolve):\n"
        "    sections['skill_usage'] = resolve('arcagent', 'skill_usage_instruction')\n"
    )
    hits = _findings(_Module(ast.parse(source)), "probe")
    assert sorted(h.sink for h in hits) == [
        "ChoiceSpec(instructions=)",
        "invoke(prompt)",
        "run_oneshot(system=)",
        "sections[...] =",
        "system_message()",
    ], [str(h) for h in hits]


def test_no_inline_model_prompts_outside_context_folders() -> None:
    """Every model-bound instruction text must come from ``<package>/context/*.md``."""
    findings = [
        hit for path in _production_files() for hit in _findings(_Module(_parse(path)), _rel(path))
    ]
    assert not findings, (
        f"{len(findings)} inline prompt string(s) sent to a model from Python — move each "
        "into its package's context/ folder and load it through the PromptSource:\n  "
        + "\n  ".join(str(f) for f in findings)
    )
