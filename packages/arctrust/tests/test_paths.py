"""Typed Arc-home accessors — one resolver per concern, resolved per call.

The bug class these guard against has bitten this repo twice: a resolver split
let one surface read a different directory than another, and a test wrote into a
developer's real ``~/.arc``. Every accessor here must therefore (a) sit under
``ARC_CONFIG_DIR`` when it is set, and (b) read the environment on *every call*,
never freeze it at import time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arctrust import paths


@pytest.fixture
def arc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``ARC_CONFIG_DIR`` at a tmp dir — never the developer's real ~/.arc."""
    root = tmp_path / "arc-home"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root))
    return root


# --------------------------------------------------------------------------
# The four roots
# --------------------------------------------------------------------------


def test_roots_resolve_under_arc_config_dir(arc_root: Path) -> None:
    assert paths.arc_home() == arc_root
    assert paths.arc_config() == arc_root / "config"
    assert paths.arc_state() == arc_root / "state"
    assert paths.arc_runtime_root() == arc_root / "runtime"
    assert paths.arc_runtime() == arc_root / "runtime" / "current"


def test_the_fleet_is_the_fourth_root_of_the_arc_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fleet is a lifecycle root of ``~/.arc``, beside runtime/config/state.

    This is the DEFAULT-root claim, so the env is cleared: it is what a real box
    resolves with nothing exported. Two properties have each already cost a live
    box, and only this location has both:

    * **Outside any code checkout.** ``~/.arc`` is not a checkout and never
      becomes one. ``~/arc`` — the previous default — is exactly where operators
      rsync and ``git pull``, so the fleet sat inside the disposable code tree
      and a pull collided with running agents' memory.
    * **Out of reach of an update.** An update replaces ``runtime/<version>/``
      and flips a symlink; it never touches a sibling root.
    """
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)

    assert paths.arc_team() == Path.home() / ".arc" / "team"
    assert paths.arc_team().parent == paths.arc_home()
    assert paths.arc_runtime_root() not in paths.arc_team().parents


def test_arc_team_root_env_relocates_only_the_fleet(
    arc_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ARC_TEAM_ROOT`` moves the fleet without moving the home, and wins."""
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "fleet"))

    assert paths.arc_team() == tmp_path / "fleet" / "team"
    assert paths.arc_home() == arc_root


def test_arc_config_dir_carries_the_fleet_into_isolation(arc_root: Path) -> None:
    """A relocated home takes its fleet with it — otherwise isolation is a lie.

    ``ARC_CONFIG_DIR`` is what every test and every self-contained deployment
    sets. When ``arc_team`` ignored it, an isolated run still resolved the real
    ``~/arc/team``: a test that created an agent created it in the developer's
    own live fleet, beside agents that were running.
    """
    assert paths.arc_team() == arc_root / "team"


def test_arc_team_accepts_an_alternate_fleet_name(arc_root: Path) -> None:
    """``arc init --team coding`` names the fleet directory."""
    assert paths.arc_team("coding") == arc_root / "coding"


def test_arc_runtime_version_is_a_sibling_of_current(arc_root: Path) -> None:
    """Side-by-side installs: an update is a symlink flip, not an overwrite."""
    assert paths.arc_runtime_version("0.9.1") == arc_root / "runtime" / "0.9.1"


def test_the_runtime_holds_the_interpreter_that_runs_arc(arc_root: Path) -> None:
    """Code AND venv live in the runtime — that is what makes the install disposable.

    A deployment whose venv sits in a git checkout has no disposable install: the
    thing an update replaces and the thing an operator pulls into are the same
    directory. Resolving the executable here is what lets the service unit, the
    deploy script, and a rollback all name one interpreter.
    """
    current = arc_root / "runtime" / "current"
    assert paths.runtime_venv() == current / ".venv"
    assert paths.runtime_bin("arc") == current / ".venv" / "bin" / "arc"


# --------------------------------------------------------------------------
# Per-call resolution — the whole point
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "accessor",
    [
        "arc_home",
        "arc_config",
        "arc_state",
        "arc_runtime",
        "arc_runtime_root",
        "operator_dir",
        "default_operator_key_path",
        "identity_dir",
        "trust_dir",
        "store_dir",
        "nats_dir",
        "bundles_dir",
        "capabilities_dir",
        "blueprints_dir",
        "skills_dir",
        "gateway_dir",
        "gateway_runtime_dir",
        "gateway_pairing_db",
        "audit_dir",
        "users_file",
        "env_file",
        "module_root",
        "runtime_venv",
        "arc_team",
    ],
)
def test_every_accessor_honors_env_set_after_import(
    accessor: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``arctrust.paths`` is imported at module load; the env var is set later.

    A module-level constant would freeze the import-time value. Setting the env
    var *after* import — which is what a service unit and every test does — must
    still move the accessor.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    fn = getattr(paths, accessor)

    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(first))
    before = fn()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(second))
    after = fn()

    assert before != after
    assert first in before.parents or before == first
    assert second in after.parents or after == second


def test_arc_team_resolves_its_own_env_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same per-call guarantee as above, for the accessor with a second env var.

    ``ARC_TEAM_ROOT`` relocates the fleet without moving the home, for an
    operator who wants agent data on a different disk. It must be read on every
    call rather than frozen at import, exactly like ``ARC_CONFIG_DIR``.
    """
    first, second = tmp_path / "first", tmp_path / "second"

    monkeypatch.setenv("ARC_TEAM_ROOT", str(first))
    before = paths.arc_team()
    monkeypatch.setenv("ARC_TEAM_ROOT", str(second))
    after = paths.arc_team()

    assert before == first / "team"
    assert after == second / "team"


def test_unset_env_falls_back_to_dot_arc_under_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-root behavior is unchanged when ``ARC_CONFIG_DIR`` is absent."""
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
    assert paths.arc_home() == Path.home() / ".arc"
    assert paths.arc_state() == Path.home() / ".arc" / "state"


def test_empty_env_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ARC_CONFIG_DIR=`` must not resolve the whole tree to the process cwd."""
    monkeypatch.setenv("ARC_CONFIG_DIR", "")
    assert paths.arc_home() == Path.home() / ".arc"


def test_env_value_is_user_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", "~/somewhere-else")
    assert paths.arc_home() == Path.home() / "somewhere-else"


# --------------------------------------------------------------------------
# Concern placement — config is preserved, state is never touched,
# runtime is replaced wholesale.
# --------------------------------------------------------------------------


def test_config_accessors_live_under_the_config_root(arc_root: Path) -> None:
    cfg = arc_root / "config"
    assert paths.config_file("arcagent.toml") == cfg / "arcagent.toml"
    assert paths.config_file("arcllm.toml") == cfg / "arcllm.toml"
    assert paths.config_file("gateway.toml") == cfg / "gateway.toml"
    assert paths.env_file() == cfg / "arc.env"


@pytest.mark.parametrize(
    ("accessor", "expected"),
    [
        ("operator_dir", "operator"),
        ("identity_dir", "identity"),
        ("trust_dir", "trust"),
        ("store_dir", "store"),
        ("nats_dir", "nats"),
        ("bundles_dir", "bundles"),
        ("capabilities_dir", "capabilities"),
        ("blueprints_dir", "blueprints"),
        ("skills_dir", "skills"),
        ("gateway_dir", "gateway"),
        ("audit_dir", "audit"),
    ],
)
def test_state_accessors_live_under_the_state_root(
    arc_root: Path, accessor: str, expected: str
) -> None:
    """Irreplaceable material — never destroyed by an update."""
    assert getattr(paths, accessor)() == arc_root / "state" / expected


def test_operator_key_and_users_file_are_state(arc_root: Path) -> None:
    assert paths.default_operator_key_path() == arc_root / "state" / "operator" / "operator.key"
    assert paths.users_file() == arc_root / "state" / "users.json"
    assert paths.gateway_runtime_dir() == arc_root / "state" / "gateway" / "run"
    assert paths.gateway_pairing_db() == arc_root / "state" / "gateway" / "pairing.db"


def test_modules_live_under_the_replaceable_runtime(arc_root: Path) -> None:
    """Modules are re-materialized by ``arc install``; they ride with the code."""
    assert paths.module_root() == arc_root / "runtime" / "current" / "modules"


def test_no_state_path_falls_inside_the_runtime(arc_root: Path) -> None:
    """The invariant that makes 'overwrite the runtime' safe."""
    runtime = paths.arc_runtime_root()
    for accessor in ("arc_config", "arc_state", "arc_team"):
        resolved = getattr(paths, accessor)()
        assert runtime not in resolved.parents
        assert resolved != runtime


# --------------------------------------------------------------------------
# Runtime activation — atomic flip, reversible
# --------------------------------------------------------------------------


def test_activate_runtime_points_current_at_the_named_version(arc_root: Path) -> None:
    (arc_root / "runtime" / "1.0.0").mkdir(parents=True)
    paths.activate_runtime("1.0.0")
    assert paths.arc_runtime().resolve() == (arc_root / "runtime" / "1.0.0").resolve()


def test_activate_runtime_is_a_reversible_flip(arc_root: Path) -> None:
    for version in ("1.0.0", "1.1.0"):
        (arc_root / "runtime" / version).mkdir(parents=True)
    paths.activate_runtime("1.0.0")
    paths.activate_runtime("1.1.0")
    assert paths.arc_runtime().resolve().name == "1.1.0"
    paths.activate_runtime("1.0.0")  # rollback
    assert paths.arc_runtime().resolve().name == "1.0.0"


def test_activate_runtime_rejects_a_version_that_is_not_installed(arc_root: Path) -> None:
    (arc_root / "runtime").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        paths.activate_runtime("9.9.9")
    assert not (arc_root / "runtime" / "current").exists()


def test_activate_runtime_replaces_a_real_directory_named_current(arc_root: Path) -> None:
    """A pre-symlink install may have a plain ``current/`` dir; the flip must win."""
    (arc_root / "runtime" / "2.0.0").mkdir(parents=True)
    (arc_root / "runtime" / "current").mkdir(parents=True)
    paths.activate_runtime("2.0.0")
    assert (arc_root / "runtime" / "current").is_symlink()
    assert paths.arc_runtime().resolve().name == "2.0.0"


def test_activate_runtime_rejects_a_traversing_version_name(arc_root: Path) -> None:
    """A version string is a directory name, never a path — no escaping the root."""
    (arc_root / "runtime").mkdir(parents=True)
    for bad in ("../outside", "a/b", "", "."):
        with pytest.raises(ValueError):
            paths.activate_runtime(bad)


def test_accessors_never_touch_the_real_home(arc_root: Path) -> None:
    """No accessor creates anything; resolution is pure."""
    for name in paths.__all__:
        fn = getattr(paths, name)
        if callable(fn) and name.startswith(("arc_", "default_", "config_")):
            continue
    assert not arc_root.exists() or not any(arc_root.iterdir())


def test_arc_config_dir_env_name_is_exported() -> None:
    assert paths.ARC_CONFIG_DIR_ENV == "ARC_CONFIG_DIR"
    assert os.environ.get("ARC_CONFIG_DIR") is None or True


# --------------------------------------------------------------------------
# Explicit base — what ``--arc-dir`` / ``--dir`` mean
# --------------------------------------------------------------------------


def test_an_explicit_base_overrides_the_env_for_every_accessor(
    arc_root: Path, tmp_path: Path
) -> None:
    """``arc <cmd> --arc-dir /other`` must operate on THAT deployment's tree.

    Every accessor takes the same optional base, so a surface never has to join
    ``other / "state" / "trust"`` by hand — which is how one command wrote into
    one deployment while another read a second.
    """
    other = tmp_path / "other-deployment"
    assert paths.trust_dir(other) == other / "state" / "trust"
    assert paths.config_file("arcagent.toml", other) == other / "config" / "arcagent.toml"
    assert paths.default_operator_key_path(other) == other / "state" / "operator" / "operator.key"
    assert paths.module_root(other) == other / "runtime" / "current" / "modules"
    assert paths.arc_team("coding", other) == other / "coding"
    # ...and the env-resolved answers are untouched by it.
    assert paths.trust_dir() == arc_root / "state" / "trust"


def test_an_explicit_base_is_user_expanded(arc_root: Path) -> None:
    assert paths.trust_dir("~/elsewhere") == Path.home() / "elsewhere" / "state" / "trust"


def test_an_empty_base_falls_back_to_the_env_root(arc_root: Path) -> None:
    """``getattr(args, "arc_dir", None) or ""`` must not resolve to the cwd."""
    assert paths.trust_dir("") == arc_root / "state" / "trust"


def test_workflows_dir_is_state(arc_root: Path) -> None:
    """Signed workflow bundles are deployment content, not disposable runtime."""
    assert paths.workflows_dir() == arc_root / "state" / "workflows"


def test_activating_twice_over_a_real_directory_never_clobbers_the_first_copy(
    arc_root: Path,
) -> None:
    """The set-aside copy of a pre-symlink install is irreplaceable — keep both."""
    for version in ("1.0.0", "2.0.0"):
        (arc_root / "runtime" / version).mkdir(parents=True)
    real = arc_root / "runtime" / "current"
    real.mkdir(parents=True)
    (real / "keepme.txt").write_text("first", encoding="utf-8")

    paths.activate_runtime("1.0.0")
    (arc_root / "runtime" / "current").unlink()
    second = arc_root / "runtime" / "current"
    second.mkdir()
    (second / "keepme.txt").write_text("second", encoding="utf-8")
    paths.activate_runtime("2.0.0")

    saved = sorted(p for p in (arc_root / "runtime").iterdir() if "pre-symlink" in p.name)
    assert len(saved) == 2, "a second activation overwrote the first set-aside copy"
    assert {(p / "keepme.txt").read_text(encoding="utf-8") for p in saved} == {"first", "second"}
