"""SPEC-083 T-1218 (COMP-027; REQ-508, REQ-510, REQ-511) — ``arc agent promotion``.

Command surface (``arc agent memory`` is a flat, read-only DB view with a
positional path and no subcommand tree, so the SDD's ``arc agent promotion``
is used)::

    arc agent promotion show <agent_dir> [--json] [--arc-dir D] [--data-dir D]
    arc agent promotion set  <agent_dir> [--enabled|--disabled]
                             [--threshold X] [--model M] [--arc-dir D] [--data-dir D]
    arc agent promotion key set    [--arc-dir D] [--data-dir D]   # stdin or getpass
    arc agent promotion key remove [--arc-dir D] [--data-dir D]

Properties pinned here:

* ``show`` prints the settings, the tier, and ``key: set`` / ``key: not set`` —
  never the key value.
* ``set`` writes only ``[modules.memory.config.promotion]`` and preserves every
  other byte of meaning in the TOML, comments included.
* A refused change (threshold below the 0.90 floor, a ``*-latest`` model, or
  ``enabled`` on a federal agent) exits non-zero and leaves the file
  BYTE-IDENTICAL and the audit chain untouched.
* Every accepted change emits exactly one ``memory.promotion.config_changed``
  event on the operator-signed WORM chain, naming the operator, the agent and
  each changed field old -> new.
* ``key set`` stores ``TYPESAFE_API_KEY`` through the one write-only KeyStore;
  the value never reaches stdout, stderr, the audit chain or agent TOML.

Audit is asserted against the REAL chain (``<data>/worm/audit-chain.jsonl``),
unsealed with the operator record cipher — not against a stub sink — so a
command that forgets to open the chain cannot pass.
"""

from __future__ import annotations

import asyncio
import getpass
import io
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from arcagent.keys import KeyStore, default_env_file
from arcagent.scaffold import render_agent_config

from arccli.commands.agent import agent_handler

JEV_KEY = "TYPESAFE_API_KEY"
KEY_VALUE = "ts-live-do-not-leak-me-91c0ffee"
CONFIG_CHANGED = "memory.promotion.config_changed"
AGENT_DID = "did:arc:local:executor/olivia01"

_AGENT_TOML = """\
# Olivia — hand-tuned by the operator. This comment must survive a write.
[agent]
name = "olivia"
org = "local"

[identity]
did = "{did}"

[security]
tier = "{tier}"  # tier comment must survive too

[modules.memory]
enabled = true
priority = 100

[modules.memory.config]
brain = "arcmemory"
tier = "{tier}"
top_k = 9  # operator tuned

[modules.memory.config.promotion]
enabled = false
confidence_threshold = 0.95
classifier_model = "jev-1.13.0"
max_items_per_sweep = 50  # unrelated promotion knob must survive

[modules.connectors]
enabled = true
priority = 100
"""


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    path = tmp_path / "arc"
    path.mkdir()
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def world(arc_dir: Path, data_dir: Path) -> list[str]:
    return ["--arc-dir", str(arc_dir), "--data-dir", str(data_dir)]


def _make_agent(root: Path, *, tier: str = "personal", body: str | None = None) -> Path:
    agent_dir = root / "team" / f"olivia-{tier}"
    agent_dir.mkdir(parents=True)
    text = body if body is not None else _AGENT_TOML.format(did=AGENT_DID, tier=tier)
    (agent_dir / "arcagent.toml").write_text(text, encoding="utf-8")
    return agent_dir


@pytest.fixture
def personal_agent(tmp_path: Path) -> Path:
    return _make_agent(tmp_path, tier="personal")


@pytest.fixture
def federal_agent(tmp_path: Path) -> Path:
    return _make_agent(tmp_path, tier="federal")


def _run(*argv: str) -> int:
    """Invoke ``arc agent <argv>`` in-process; return the exit code (0 when none)."""
    try:
        agent_handler(list(argv))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def _toml(agent_dir: Path) -> dict[str, Any]:
    return tomllib.loads((agent_dir / "arcagent.toml").read_text(encoding="utf-8"))


def _promotion(agent_dir: Path) -> dict[str, Any]:
    return dict(_toml(agent_dir)["modules"]["memory"]["config"]["promotion"])


def _chain_path(data_dir: Path) -> Path:
    from arcstore.ingest import WORM_ACTIVE_FILENAME

    return data_dir / "worm" / WORM_ACTIVE_FILENAME


def _chain_raw(data_dir: Path) -> str:
    path = _chain_path(data_dir)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _events(arc_dir: Path, data_dir: Path) -> list[dict[str, Any]]:
    """Every event on the operator chain, with sealed content opened."""
    from arccli.commands.operator import resolve_record_cipher

    raw = _chain_raw(data_dir)
    if not raw:
        return []
    cipher = resolve_record_cipher(arc_dir)
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)["event"]
        events.append(cipher.unseal(event) if cipher is not None else event)
    return events


def _config_events(arc_dir: Path, data_dir: Path) -> list[dict[str, Any]]:
    return [e for e in _events(arc_dir, data_dir) if e["action"] == CONFIG_CHANGED]


def _store_key(arc_dir: Path) -> None:
    asyncio.run(KeyStore(default_env_file(arc_dir)).set(JEV_KEY, KEY_VALUE))


class _TTYStdin(io.StringIO):
    """A terminal stdin: the command must prompt with getpass, not read it."""

    def isatty(self) -> bool:
        return True

    def read(self, *_: Any) -> str:  # pragma: no cover - fails the test if hit
        pytest.fail("a key on a terminal must be collected with getpass, not read()")

    def readline(self, *_: Any) -> str:  # pragma: no cover - fails the test if hit
        pytest.fail("a key on a terminal must be collected with getpass, not readline()")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_promotion_is_an_arc_agent_subcommand() -> None:
    from arccli.commands.agent._dispatch import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["promotion", "show", "."])
    assert args.subcmd == "promotion"


# ---------------------------------------------------------------------------
# show — settings, tier, key presence; never a value
# ---------------------------------------------------------------------------


def test_show_prints_settings_tier_and_key_not_set(
    personal_agent: Path, world: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run("promotion", "show", str(personal_agent), *world)

    out = capsys.readouterr().out
    assert code == 0
    assert "enabled: false" in out.lower()
    assert "0.95" in out
    assert "jev-1.13.0" in out
    assert "personal" in out
    assert "key: not set" in out


def test_show_reports_key_set_and_never_prints_the_value(
    personal_agent: Path, arc_dir: Path, world: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _store_key(arc_dir)

    code = _run("promotion", "show", str(personal_agent), *world)

    captured = capsys.readouterr()
    assert code == 0
    assert "key: set" in captured.out
    assert "key: not set" not in captured.out
    assert KEY_VALUE not in captured.out
    assert KEY_VALUE not in captured.err


def test_show_json_has_the_shared_settings_shape_and_no_value(
    federal_agent: Path, arc_dir: Path, world: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _store_key(arc_dir)

    code = _run("promotion", "show", str(federal_agent), "--json", *world)

    out = capsys.readouterr().out
    assert code == 0
    assert KEY_VALUE not in out
    payload = json.loads(out)
    assert payload == {
        "enabled": False,
        "confidence_threshold": 0.95,
        "classifier_model": "jev-1.13.0",
        "tier": "federal",
        "federal_locked": True,
        "key_set": True,
    }


def test_show_uses_defaults_when_the_agent_has_no_promotion_block(
    tmp_path: Path, world: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    body = _AGENT_TOML.format(did=AGENT_DID, tier="personal").split(
        "[modules.memory.config.promotion]"
    )[0]
    agent_dir = _make_agent(tmp_path, body=body)

    code = _run("promotion", "show", str(agent_dir), "--json", *world)

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["enabled"] is False
    assert payload["confidence_threshold"] == 0.95
    assert payload["classifier_model"] == "jev-1.13.0"
    assert payload["federal_locked"] is False


def test_show_on_a_directory_without_an_agent_config_exits_nonzero(
    tmp_path: Path, world: list[str]
) -> None:
    empty = tmp_path / "not-an-agent"
    empty.mkdir()
    assert _run("promotion", "show", "--help") == 0, "precondition: the verb exists"
    assert _run("promotion", "show", str(empty), *world) != 0


# ---------------------------------------------------------------------------
# set — writes the promotion block, preserves everything else
# ---------------------------------------------------------------------------


def test_set_writes_the_promotion_block(personal_agent: Path, world: list[str]) -> None:
    code = _run(
        "promotion", "set", str(personal_agent),
        "--enabled", "--threshold", "0.96", "--model", "jev-1.13.0",
        *world,
    )  # fmt: skip

    assert code == 0
    block = _promotion(personal_agent)
    assert block["enabled"] is True
    assert block["confidence_threshold"] == 0.96
    assert block["classifier_model"] == "jev-1.13.0"


def test_set_preserves_every_other_setting_and_comment(
    personal_agent: Path, world: list[str]
) -> None:
    before_text = (personal_agent / "arcagent.toml").read_text(encoding="utf-8")
    before = _toml(personal_agent)

    assert _run("promotion", "set", str(personal_agent), "--threshold", "0.97", *world) == 0

    after_text = (personal_agent / "arcagent.toml").read_text(encoding="utf-8")
    after = _toml(personal_agent)
    # Comments survive (tomlkit round-trip, not a re-render).
    for line in before_text.splitlines():
        if "#" in line and "confidence_threshold" not in line:
            assert line in after_text, f"lost: {line!r}"
    # Every non-promotion value is unchanged.
    before_promo = before["modules"]["memory"]["config"].pop("promotion")
    after_promo = after["modules"]["memory"]["config"].pop("promotion")
    assert after == before
    # Within the promotion block only the named field moved.
    assert after_promo == {**before_promo, "confidence_threshold": 0.97}


def test_set_creates_the_promotion_block_when_absent(tmp_path: Path, world: list[str]) -> None:
    body = _AGENT_TOML.format(did=AGENT_DID, tier="personal").split(
        "[modules.memory.config.promotion]"
    )[0]
    agent_dir = _make_agent(tmp_path, body=body)

    assert _run("promotion", "set", str(agent_dir), "--enabled", *world) == 0

    assert _promotion(agent_dir)["enabled"] is True
    assert _toml(agent_dir)["modules"]["memory"]["config"]["top_k"] == 9


def test_set_disabled_turns_promotion_off(personal_agent: Path, world: list[str]) -> None:
    assert _run("promotion", "set", str(personal_agent), "--enabled", *world) == 0
    assert _run("promotion", "set", str(personal_agent), "--disabled", *world) == 0
    assert _promotion(personal_agent)["enabled"] is False


def test_set_never_writes_a_key_into_agent_toml(
    personal_agent: Path, arc_dir: Path, world: list[str]
) -> None:
    _store_key(arc_dir)
    assert _run("promotion", "set", str(personal_agent), "--enabled", *world) == 0
    text = (personal_agent / "arcagent.toml").read_text(encoding="utf-8")
    assert KEY_VALUE not in text


def test_set_has_no_flag_that_accepts_a_key_value(personal_agent: Path, world: list[str]) -> None:
    # A credential on argv is shell history and a process-table entry (D-555).
    assert _run("promotion", "set", "--help") == 0, "precondition: the verb exists"
    for flag in ("--key", "--api-key", "--value"):
        before = (personal_agent / "arcagent.toml").read_bytes()
        assert _run("promotion", "set", str(personal_agent), flag, KEY_VALUE, *world) != 0
        assert (personal_agent / "arcagent.toml").read_bytes() == before


# ---------------------------------------------------------------------------
# set — refusals: non-zero exit, file byte-identical, no audit event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "names"),
    [
        (["--threshold", "0.85"], "threshold"),
        (["--threshold", "0.8999"], "threshold"),
        (["--threshold", "1.5"], "threshold"),
        (["--threshold", "nan"], "threshold"),
        (["--threshold", "not-a-number"], "threshold"),
        (["--model", "jev-latest"], "model"),
        (["--model", "JEV-LATEST"], "model"),
        (["--model", "latest"], "model"),
        (["--model", ""], "model"),
        (["--enabled", "--threshold", "0.85"], "threshold"),
    ],
)
def test_an_invalid_setting_is_refused_and_nothing_is_written(
    personal_agent: Path,
    arc_dir: Path,
    data_dir: Path,
    world: list[str],
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    names: str,
) -> None:
    before = (personal_agent / "arcagent.toml").read_bytes()

    code = _run("promotion", "set", str(personal_agent), *extra, *world)

    assert code != 0
    assert (personal_agent / "arcagent.toml").read_bytes() == before
    assert _config_events(arc_dir, data_dir) == []
    # The refusal names the offending field — an "invalid choice: 'promotion'"
    # from a missing subcommand must not pass as a validation refusal.
    assert names in capsys.readouterr().err.lower()


def test_the_threshold_floor_itself_is_accepted(personal_agent: Path, world: list[str]) -> None:
    # Narrowness: the floor is inclusive (ge=0.90), so a strict '>' would be a bug.
    assert _run("promotion", "set", str(personal_agent), "--threshold", "0.90", *world) == 0
    assert _promotion(personal_agent)["confidence_threshold"] == 0.90


def test_enabling_on_a_federal_agent_is_refused_and_nothing_is_written(
    federal_agent: Path,
    arc_dir: Path,
    data_dir: Path,
    world: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = (federal_agent / "arcagent.toml").read_bytes()

    code = _run("promotion", "set", str(federal_agent), "--enabled", *world)

    assert code != 0
    assert (federal_agent / "arcagent.toml").read_bytes() == before
    assert _config_events(arc_dir, data_dir) == []
    assert "federal" in capsys.readouterr().err.lower()


def test_enabling_on_a_scaffolded_federal_agent_is_refused(
    tmp_path: Path, world: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The real `arc agent create --tier federal` scaffold, not a hand-written file.
    body = render_agent_config(name="fed", tier="federal", did=AGENT_DID)
    agent_dir = _make_agent(tmp_path, tier="federal", body=body)
    before = (agent_dir / "arcagent.toml").read_bytes()

    assert _run("promotion", "set", str(agent_dir), "--enabled", *world) != 0
    assert (agent_dir / "arcagent.toml").read_bytes() == before
    assert "federal" in capsys.readouterr().err.lower()


def test_enabling_is_refused_when_only_the_security_tier_is_federal(
    tmp_path: Path, world: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Fail closed: [security].tier is the agent's tier. A memory block that
    # forgot to say federal must not become a way to egress at federal.
    body = _AGENT_TOML.format(did=AGENT_DID, tier="federal").replace(
        'brain = "arcmemory"\ntier = "federal"\n', 'brain = "arcmemory"\n'
    )
    agent_dir = _make_agent(tmp_path, tier="federal", body=body)
    before = (agent_dir / "arcagent.toml").read_bytes()

    assert _run("promotion", "set", str(agent_dir), "--enabled", *world) != 0
    assert (agent_dir / "arcagent.toml").read_bytes() == before
    assert "federal" in capsys.readouterr().err.lower()


def test_a_tampered_federal_block_already_enabled_refuses_any_further_write(
    tmp_path: Path, world: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The MERGED block is validated, not just the flags on this call.
    body = _AGENT_TOML.format(did=AGENT_DID, tier="federal").replace(
        "[modules.memory.config.promotion]\nenabled = false",
        "[modules.memory.config.promotion]\nenabled = true",
    )
    agent_dir = _make_agent(tmp_path, tier="federal", body=body)
    before = (agent_dir / "arcagent.toml").read_bytes()

    assert _run("promotion", "set", str(agent_dir), "--threshold", "0.97", *world) != 0
    assert (agent_dir / "arcagent.toml").read_bytes() == before
    assert "federal" in capsys.readouterr().err.lower()


def test_a_federal_agent_may_still_tune_a_disabled_block(
    federal_agent: Path, world: list[str]
) -> None:
    # Narrowness: federal refuses ENABLED, not every write.
    assert _run("promotion", "set", str(federal_agent), "--threshold", "0.97", *world) == 0
    block = _promotion(federal_agent)
    assert block["confidence_threshold"] == 0.97
    assert block["enabled"] is False


# ---------------------------------------------------------------------------
# audit — one event per accepted change, operator + agent + old -> new
# ---------------------------------------------------------------------------


def test_an_accepted_change_emits_exactly_one_config_changed_event(
    personal_agent: Path, arc_dir: Path, data_dir: Path, world: list[str]
) -> None:
    code = _run(
        "promotion", "set", str(personal_agent),
        "--enabled", "--threshold", "0.96",
        *world,
    )  # fmt: skip

    assert code == 0
    events = _config_events(arc_dir, data_dir)
    assert len(events) == 1
    event = events[0]
    assert event["actor_did"].startswith("did:arc:local:operator")
    assert event["outcome"] == "allow"
    assert AGENT_DID in event["target"] or "olivia" in event["target"]
    assert event["extra"]["changes"] == {
        "enabled": {"old": False, "new": True},
        "confidence_threshold": {"old": 0.95, "new": 0.96},
    }


def test_each_change_is_its_own_event(
    personal_agent: Path, arc_dir: Path, data_dir: Path, world: list[str]
) -> None:
    assert _run("promotion", "set", str(personal_agent), "--threshold", "0.96", *world) == 0
    assert _run("promotion", "set", str(personal_agent), "--model", "jev-1.14", *world) == 0

    changes = [e["extra"]["changes"] for e in _config_events(arc_dir, data_dir)]
    assert changes == [
        {"confidence_threshold": {"old": 0.95, "new": 0.96}},
        {"classifier_model": {"old": "jev-1.13.0", "new": "jev-1.14"}},
    ]


def test_the_config_event_is_on_the_signed_chain_in_the_clear_envelope(
    personal_agent: Path, data_dir: Path, world: list[str]
) -> None:
    # The action is envelope (searchable); the content is sealed at rest.
    assert _run("promotion", "set", str(personal_agent), "--enabled", *world) == 0
    raw = _chain_raw(data_dir)
    assert CONFIG_CHANGED in raw
    assert '"changes"' not in raw, "old -> new content must be sealed at rest"


# ---------------------------------------------------------------------------
# key — the fleet-wide TYPESAFE_API_KEY through the one KeyStore
# ---------------------------------------------------------------------------


def _entries(arc_dir: Path) -> dict[str, str]:
    env_file = default_env_file(arc_dir)
    if not env_file.exists():
        return {}
    lines = env_file.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_key_set_reads_piped_stdin_and_stores_via_the_keystore(
    monkeypatch: pytest.MonkeyPatch,
    arc_dir: Path,
    data_dir: Path,
    world: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY_VALUE + "\n"))

    code = _run("promotion", "key", "set", *world)

    captured = capsys.readouterr()
    assert code == 0
    assert _entries(arc_dir) == {JEV_KEY: KEY_VALUE}
    assert KEY_VALUE not in captured.out
    assert KEY_VALUE not in captured.err


def test_key_set_on_a_terminal_prompts_with_getpass(
    monkeypatch: pytest.MonkeyPatch,
    arc_dir: Path,
    world: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def fake_getpass(prompt: str = "", stream: Any = None) -> str:
        prompts.append(prompt)
        return KEY_VALUE

    monkeypatch.setattr("sys.stdin", _TTYStdin())
    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    monkeypatch.setattr(
        "builtins.input", lambda *_: pytest.fail("a key must never be read with input()")
    )

    code = _run("promotion", "key", "set", *world)

    captured = capsys.readouterr()
    assert code == 0
    assert len(prompts) == 1
    assert _entries(arc_dir) == {JEV_KEY: KEY_VALUE}
    assert KEY_VALUE not in captured.out + captured.err


def test_key_set_is_audited_by_coordinate_never_by_value(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, data_dir: Path, world: list[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY_VALUE + "\n"))

    assert _run("promotion", "key", "set", *world) == 0

    raw = _chain_raw(data_dir)
    assert KEY_VALUE not in raw
    writes = [e for e in _events(arc_dir, data_dir) if e["action"] == "provider_key.write"]
    assert len(writes) == 1
    assert JEV_KEY in writes[0]["target"]
    assert writes[0]["actor_did"].startswith("did:arc:local:operator")
    for event in _events(arc_dir, data_dir):
        assert KEY_VALUE not in json.dumps(event, default=str)


def test_key_set_refuses_an_empty_value_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, arc_dir: Path, world: list[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    assert _run("promotion", "key", "set", *world) == 1
    assert _entries(arc_dir) == {}


def test_key_set_has_no_flag_that_accepts_a_value(world: list[str], arc_dir: Path) -> None:
    assert _run("promotion", "key", "set", "--help") == 0, "precondition: the verb exists"
    assert _run("promotion", "key", "set", "--value", KEY_VALUE, *world) != 0
    assert _entries(arc_dir) == {}


def test_key_set_then_show_reports_set_without_the_value(
    monkeypatch: pytest.MonkeyPatch,
    personal_agent: Path,
    world: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY_VALUE + "\n"))
    assert _run("promotion", "key", "set", *world) == 0
    capsys.readouterr()

    assert _run("promotion", "show", str(personal_agent), *world) == 0
    out = capsys.readouterr().out
    assert "key: set" in out
    assert KEY_VALUE not in out
    assert KEY_VALUE not in (personal_agent / "arcagent.toml").read_text(encoding="utf-8")


def test_key_remove_forgets_the_key_and_is_audited(
    arc_dir: Path, data_dir: Path, personal_agent: Path, world: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    _store_key(arc_dir)

    assert _run("promotion", "key", "remove", *world) == 0
    assert JEV_KEY not in _entries(arc_dir)
    deletes = [e for e in _events(arc_dir, data_dir) if e["action"] == "provider_key.delete"]
    assert len(deletes) == 1
    assert JEV_KEY in deletes[0]["target"]
    capsys.readouterr()

    assert _run("promotion", "show", str(personal_agent), *world) == 0
    assert "key: not set" in capsys.readouterr().out
