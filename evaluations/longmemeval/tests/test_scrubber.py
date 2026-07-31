"""ArtifactScrubber tests — COMP-015 / REQ-197.

Two invariants are under test here, and they are the two halves of the same
requirement: nothing secret-shaped reaches the results JSONL, and no API key
ever comes off disk in the first place.

The second half is checked against the harness sources themselves rather than
against one module's behaviour. A behavioural test proves the module it calls;
the requirement is about every module, including the ones written after this
test. So `test_no_api_key_is_ever_read_from_a_file` parses every non-test
module under `evaluations/` and asserts that an API-key environment variable
name is only ever used to query `os.environ` — never to index a parsed file, and
never as an argument to a file-reading primitive.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import arcmemory.security

from evaluations.longmemeval import scrub
from evaluations.longmemeval.scrub import ArtifactScrubber

# An OpenAI-shaped key long enough to match arcmemory's `sk-[A-Za-z0-9]{16,}`.
FAKE_KEY = "sk-A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
REDACTED = "[REDACTED]"


def test_the_real_privacy_filter_is_used_not_a_local_copy() -> None:
    """A second secret-pattern list is a second list that drifts."""
    assert vars(scrub)["privacy_filter"] is arcmemory.security.privacy_filter


def test_scrub_row_filters_the_agent_answer() -> None:
    scrubber = ArtifactScrubber()
    row: dict[str, Any] = {"answer": f"You told me your key is {FAKE_KEY} last March."}

    scrubber.scrub_row(row)

    assert FAKE_KEY not in row["answer"]
    assert REDACTED in row["answer"]


def test_scrub_row_filters_the_judge_raw_response_and_prompt() -> None:
    """`prompt_used` embeds the agent answer verbatim, so it leaks the same text."""
    scrubber = ArtifactScrubber()
    row: dict[str, Any] = {
        "verdict": {
            "correct": False,
            "raw_response": f"no — the answer quotes {FAKE_KEY}",
            "prompt_used": f"My answer: the key is {FAKE_KEY}\nIs it correct?",
            "judge_model_id": "openai/gpt-4o-2024-08-06",
        }
    }

    scrubber.scrub_row(row)

    assert FAKE_KEY not in row["verdict"]["raw_response"]
    assert FAKE_KEY not in row["verdict"]["prompt_used"]
    assert REDACTED in row["verdict"]["raw_response"]
    assert REDACTED in row["verdict"]["prompt_used"]
    # Non-text verdict fields are untouched.
    assert row["verdict"]["correct"] is False
    assert row["verdict"]["judge_model_id"] == "openai/gpt-4o-2024-08-06"


def test_scrub_exception_filters_provider_error_bodies_that_echo_request_context() -> None:
    """The case people forget: the error body hands the request back to you."""
    scrubber = ArtifactScrubber()
    exc = RuntimeError(
        "401 Unauthorized from provider: "
        f'{{"request": {{"headers": {{"authorization": "Bearer {FAKE_KEY}"}}, '
        f'"api_key": "{FAKE_KEY}"}}}}'
    )

    text = scrubber.scrub_exception(exc)

    assert FAKE_KEY not in text
    assert REDACTED in text
    # The class still identifies the failure — only the message can echo a body.
    assert text.startswith("RuntimeError:")


def test_scrub_row_filters_exception_text_already_placed_on_the_row() -> None:
    """An `error` row built elsewhere still passes through the same gate."""
    scrubber = ArtifactScrubber()
    row: dict[str, Any] = {
        "status": "error",
        "error": f"APIStatusError: request rejected, api_key={FAKE_KEY}",
    }

    scrubber.scrub_row(row)

    assert FAKE_KEY not in row["error"]
    assert REDACTED in row["error"]


def test_scrub_row_leaves_harness_computed_fields_byte_for_byte() -> None:
    """Provenance is evidence; filtering it would corrupt what it proves."""
    scrubber = ArtifactScrubber()
    provenance = {"git_sha": "a" * 40, "config_hash": "b" * 64, "tier": "personal"}
    row: dict[str, Any] = {
        "question_id": "gpt4_e5f7a1",
        "status": "complete",
        "answer": "March 2024.",
        "provenance": dict(provenance),
        "cost": {"tokens_in": 12000, "cost_usd": 0.31},
    }

    scrubber.scrub_row(row)

    assert row["provenance"] == provenance
    assert row["cost"] == {"tokens_in": 12000, "cost_usd": 0.31}
    assert row["question_id"] == "gpt4_e5f7a1"


def test_scrub_row_returns_the_same_dict_and_tolerates_missing_fields() -> None:
    """A void row has no verdict; an error row has no answer. Both get written."""
    scrubber = ArtifactScrubber()
    row: dict[str, Any] = {"question_id": "q1", "status": "void", "verdict": None}

    assert scrubber.scrub_row(row) is row
    assert row == {"question_id": "q1", "status": "void", "verdict": None}


# --------------------------------------------------------------------------
# REQ-197, second half: keys come from the environment, never from a file.
# --------------------------------------------------------------------------

EVALUATIONS_ROOT = Path(__file__).resolve().parents[2]

# An API-key *environment variable* name: uppercase, e.g. LME_JUDGE_OPENAI_API_KEY.
# Deliberately not matching lowercase `api_key_env`, which is a config field name
# in prose and TOML, not a variable a value is fetched from.
KEY_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*API_KEY[A-Z0-9_]*$")

# Calls that turn something on disk into data. A key name reaching one of these
# means the key is being pulled out of a file.
FILE_READ_FUNCS = frozenset(
    {
        "open",
        "read",
        "read_text",
        "read_bytes",
        "readline",
        "readlines",
        "load",
        "loads",
        "dotenv_values",
        "load_dotenv",
        "ConfigParser",
        "read_file",
    }
)


def _harness_sources() -> list[Path]:
    """Every non-test Python module under `evaluations/`."""
    return sorted(
        path
        for path in EVALUATIONS_ROOT.rglob("*.py")
        if "tests" not in path.relative_to(EVALUATIONS_ROOT).parts
    )


def _parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_os_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and _is_os(node.value)


def _is_os(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "os"


def _called_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _key_env_names(tree: ast.Module) -> set[str]:
    """Module constants bound to an API-key environment variable name."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        if not KEY_ENV_NAME_RE.match(value.value):
            continue
        names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _mentions_key_env_name(node: ast.Constant | ast.Name, constants: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(KEY_ENV_NAME_RE.match(node.value))
    return node.id in constants


def _retrieval_violation(node: ast.expr, parent: ast.AST | None) -> str | None:
    """Describe how `node` retrieves a value, if it is not an `os.environ` lookup."""
    if isinstance(parent, ast.Subscript) and parent.slice is node:
        if _is_os_environ(parent.value):
            return None
        return "used to index something that is not os.environ"
    if isinstance(parent, ast.Call) and node in parent.args:
        func = parent.func
        if isinstance(func, ast.Attribute):
            if func.attr == "getenv" and _is_os(func.value):
                return None
            if func.attr == "get":
                if _is_os_environ(func.value):
                    return None
                return "passed to .get() on something that is not os.environ"
        called = _called_name(func)
        if called in FILE_READ_FUNCS:
            return f"passed to the file-reading call {called}()"
    return None


class KeySourceScan:
    """What a module does with API-key environment variable names."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.env_lookups = 0
        self.key_names: set[str] = set()

    def scan(self, source: str, label: str) -> None:
        tree = ast.parse(source, filename=label)
        parents = _parent_map(tree)
        constants = _key_env_names(tree)
        self.key_names |= constants

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant | ast.Name):
                continue
            if not _mentions_key_env_name(node, constants):
                continue
            if isinstance(node, ast.Constant):
                self.key_names.add(str(node.value))
            parent = parents.get(node)
            problem = _retrieval_violation(node, parent)
            if problem is not None:
                self.violations.append(f"{label}:{node.lineno} {problem}")
            elif isinstance(parent, ast.Subscript | ast.Call):
                self.env_lookups += 1


def test_no_api_key_is_ever_read_from_a_file() -> None:
    """REQ-197: the environment is the only source of a key, in every harness module.

    Checked structurally rather than by calling one resolver, because the
    requirement covers modules that do not exist yet.
    """
    scan = KeySourceScan()
    for path in _harness_sources():
        scan.scan(
            path.read_text(encoding="utf-8"),
            str(path.relative_to(EVALUATIONS_ROOT)),
        )

    assert scan.violations == []
    # Non-vacuity: the scan must actually reach live key-handling code, or it
    # would keep passing after the code it guards was renamed away.
    assert scan.key_names, "no API-key environment variable name found to check"
    assert scan.env_lookups > 0, "no os.environ lookup found — the scan matched nothing real"


def test_the_key_source_scan_flags_a_key_read_from_a_file() -> None:
    """The guard has teeth: each file-borne key path above is a violation below."""
    scan = KeySourceScan()
    scan.scan(
        "import tomllib\n"
        "KEY_ENV = 'OPENAI_API_KEY'\n"
        "def from_toml(path):\n"
        "    return tomllib.load(path.open('rb'))[KEY_ENV]\n"
        "def from_json(blob):\n"
        "    return blob.get('LME_JUDGE_OPENAI_API_KEY')\n"
        "def from_dotenv(path):\n"
        "    return load_dotenv(path)['OPENAI_API_KEY']\n",
        "leaky.py",
    )

    assert len(scan.violations) == 3
    assert all("os.environ" in v or "file-reading" in v for v in scan.violations)


def test_the_key_source_scan_accepts_the_environment() -> None:
    scan = KeySourceScan()
    scan.scan(
        "import os\n"
        "KEY_ENV = 'OPENAI_API_KEY'\n"
        "a = os.environ[KEY_ENV]\n"
        "b = os.environ.get('LME_JUDGE_OPENAI_API_KEY', '')\n"
        "c = os.getenv(KEY_ENV)\n",
        "clean.py",
    )

    assert scan.violations == []
    assert scan.env_lookups == 3


def test_no_harness_module_imports_a_dotenv_reader() -> None:
    """`dotenv` is the standard way keys come off disk. It is not in the harness."""
    importers: list[str] = []
    for path in _harness_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "dotenv" for name in names):
                importers.append(str(path.relative_to(EVALUATIONS_ROOT)))

    assert importers == []
