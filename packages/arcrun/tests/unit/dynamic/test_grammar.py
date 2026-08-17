"""The grammar is a security boundary, so these tests are mostly refusals.

Every construct that could reach outside the script — an import, an attribute
walk, a call to something the host did not put there — must fail at *parse*
time, because a construct rejected at parse time can never reach evaluation.
"""

from __future__ import annotations

import pytest

from arcrun.dynamic import interpreter
from arcrun.dynamic.grammar import (
    ALLOWED_BUILTINS,
    ALLOWED_METHODS,
    MAX_EXPRESSION_DEPTH,
    MAX_SCRIPT_BYTES,
    ScriptSyntaxError,
    parse_script,
)


def test_parse_script_accepts_the_shape_a_real_workflow_uses() -> None:
    """Loops, conditionals, f-strings and host calls are the working set."""
    module = parse_script(
        'phase("research")\n'
        "jobs = []\n"
        'for q in ["a", "b"]:\n'
        '    jobs.append({"prompt": f"Investigate: {q}"})\n'
        "results = parallel(jobs)\n"
        'kept = [r for r in results if r["success"]]\n'
        "if not kept:\n"
        '    pause("verification", "nothing came back")\n'
        'complete({"count": len(kept)})\n'
    )
    assert module.body


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("import os", "import"),
        ("from os import path", "import"),
        ('open("/etc/passwd")', "open"),
        ('eval("1+1")', "eval"),
        ('exec("x=1")', "exec"),
        ('__import__("os")', "__import__"),
        ("getattr(x, 'y')", "getattr"),
        ("globals()", "globals"),
    ],
)
def test_parse_script_refuses_every_route_out_of_the_sandbox(source: str, fragment: str) -> None:
    """Imports and escape-hatch builtins are absent from the language."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script(source)
    assert fragment in str(excinfo.value)


def test_parse_script_refuses_a_bare_attribute_read() -> None:
    """No attribute walk means no traversal into a host object graph."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script("x = results.__class__")
    assert "attribute" in str(excinfo.value).lower()


def test_parse_script_allows_a_whitelisted_container_method() -> None:
    """Scripts stay idiomatic: the common list/dict/str methods are in.

    They are dispatched through the interpreter's own table rather than
    ``getattr``, so allowing them opens no path to an arbitrary object graph.
    """
    parse_script('items = []\nitems.append("x")\nname = "AB".lower()\n')


def test_parse_script_refuses_a_method_outside_the_table() -> None:
    """Anything not named in the table is refused, dunder or not."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script('x = "abc".__class__()')
    assert "__class__" in str(excinfo.value)


@pytest.mark.parametrize(
    "source",
    [
        "def helper(x):\n    return x\n",
        "f = lambda x: x",
        "class Thing:\n    pass\n",
        "global x",
        "with open('f') as fh:\n    pass\n",
        "try:\n    complete(1)\nexcept Exception:\n    pass\n",
        "raise ValueError('no')",
        "del x",
        "async def f():\n    pass\n",
        "yield 1",
    ],
)
def test_parse_script_refuses_constructs_outside_the_subset(source: str) -> None:
    """The allowed node set is a whitelist; everything else is a parse error."""
    with pytest.raises(ScriptSyntaxError):
        parse_script(source)


@pytest.mark.parametrize(
    "source",
    [
        'x = "notes.txt"\nx("hello")\n',
        "foo = 1\nfoo(1, 2, 3)\n",
        'scratch_write("a", "b")\nreader = "a"\ncomplete(reader("a"))\n',
        'jobs = [{"prompt": "x"}]\njobs("run them")\n',
        'for item in ["a"]:\n    item("go")\n',
    ],
)
def test_parse_script_refuses_calling_a_name_the_script_itself_bound(source: str) -> None:
    """A bound name holds a *value*, and a value is not a call target.

    The name whitelist alone was not enough: an unknown name like ``open`` was
    refused, but a name the script had assigned passed the same check and the
    interpreter dispatched the call to a host function anyway.
    """
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script(source)
    assert "not a function" in str(excinfo.value)


@pytest.mark.parametrize("name", ["agent", "complete", "scratch_read", "len", "range"])
def test_parse_script_refuses_shadowing_a_host_function_or_builtin(name: str) -> None:
    """Rebinding one would make a later call read as something it is not."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script(f'{name} = "not a function"\n')
    assert name in str(excinfo.value)


def test_parse_script_refuses_dictionary_unpacking_at_parse_time() -> None:
    """The grammar promises parse-time refusal, so run time is too late.

    ``ast.iter_child_nodes`` skips a Dict-unpacking node's missing key, so the
    generic fallback waved ``{**d}`` through to the interpreter.
    """
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script('d = {"a": 1}\ne = {**d}\n')
    assert "unpacking" in str(excinfo.value)


def test_parse_script_refuses_an_expression_nested_past_the_ceiling() -> None:
    """Statement nesting alone left the stack open: one statement can be a
    thousand-deep tree, and both the validator and the interpreter recurse it."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script("complete(" + "1 + " * (MAX_EXPRESSION_DEPTH + 20) + "1)")
    assert "nests deeper" in str(excinfo.value)


def test_the_builtin_whitelist_matches_what_the_interpreter_implements() -> None:
    """A one-sided edit yields a script that parses and then dies at run time."""
    assert ALLOWED_BUILTINS == frozenset(interpreter._BUILTINS)


def test_the_method_whitelist_matches_what_the_interpreter_dispatches() -> None:
    """Same contract for methods, across all three receiver types."""
    assert ALLOWED_METHODS == (
        interpreter._LIST_METHODS | interpreter._DICT_METHODS | interpreter._STR_METHODS
    )


def test_parse_script_refuses_an_unknown_name() -> None:
    """A name the host never defined is caught before the run, not at runtime."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script("x = time_now()")
    assert "time_now" in str(excinfo.value)


def test_parse_script_refuses_a_name_that_only_exists_after_its_use() -> None:
    """Use-before-assign is a script bug the dry run should not have to find."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script("y = x + 1\nx = 2\n")
    assert "x" in str(excinfo.value)


def test_parse_script_accepts_a_name_bound_by_a_loop_target() -> None:
    """Loop and comprehension targets bind, so they are legal reads."""
    parse_script('for item in [1, 2]:\n    log(f"{item}")\n')


def test_parse_script_refuses_a_script_over_the_size_ceiling() -> None:
    """An unbounded script is a parser denial-of-service (LLM10)."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script("log('x')\n" * (MAX_SCRIPT_BYTES // 4))
    assert "bytes" in str(excinfo.value)


def test_parse_script_refuses_deeply_nested_control_flow() -> None:
    """Nesting depth is bounded so the tree walk cannot exhaust the stack."""
    source = ""
    for depth in range(40):
        source += "    " * depth + "if True:\n"
    source += "    " * 40 + "log('deep')\n"
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script(source)
    assert "nest" in str(excinfo.value).lower()


def test_parse_script_reports_the_line_of_the_offending_construct() -> None:
    """An author (or the model, on retry) needs to know where to look."""
    with pytest.raises(ScriptSyntaxError) as excinfo:
        parse_script('log("ok")\nimport os\n')
    assert "line 2" in str(excinfo.value)


def test_parse_script_refuses_invalid_python_before_anything_else() -> None:
    """A syntax error is still a script error, not a crash."""
    with pytest.raises(ScriptSyntaxError):
        parse_script("if True\n    log('x')\n")
