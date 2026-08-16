"""One-time move of a flat ``~/.arc`` into runtime / config / state.

Everything in this module runs against a tmp dir via ``ARC_CONFIG_DIR``. The
material being moved — the operator signing key, the trust store, the arcstore
database — is irreplaceable on a real box: lose the operator key and every WORM
audit chain it signed becomes unverifiable. So the tests here are not "did the
files land in the right folder", they are "does the deployment still work".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctrust import paths
from arctrust.audit import AuditEvent, WormSink, verify_chain
from arctrust.home_migration import MigrationError, migrate_arc_home
from arctrust.operator import OperatorKey


@pytest.fixture
def flat_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pre-split ``~/.arc``: everything sitting flat at the root."""
    root = tmp_path / "arc-home"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root))
    root.mkdir()
    for name in ("arcagent.toml", "arcllm.toml", "arcrun.toml", "gateway.toml"):
        (root / name).write_text(f"# {name}\n", encoding="utf-8")
    (root / "connections.toml").write_text("[connections]\n", encoding="utf-8")
    (root / "arc.env").write_text("ANTHROPIC_API_KEY=x\n", encoding="utf-8")
    for name in ("operator", "identity", "trust", "store", "nats", "bundles", "modules"):
        (root / name).mkdir()
        (root / name / "marker.txt").write_text(name, encoding="utf-8")
    (root / "users.json").write_text("[]", encoding="utf-8")
    return root


def _seed_operator_chain(root: Path) -> tuple[bytes, str]:
    """Write a real operator key and a signed WORM chain under the FLAT layout.

    Returns the operator public key and the chain tip, so a post-migration
    verification proves both the key and the chain survived the move intact.
    """
    key = OperatorKey.generate()
    key_path = root / "operator" / "operator.key"
    key.save(key_path)
    signer = key.into_signer()
    sink = WormSink(root / "store" / "audit.jsonl", signer)
    for i in range(3):
        sink.write(
            AuditEvent(
                action="test.event", actor_did="did:arc:test", target=str(i), outcome="success"
            )
        )
    tip = sink.chain_tip
    sink.close()
    return signer.public_key, tip


# --------------------------------------------------------------------------
# The assertion that matters
# --------------------------------------------------------------------------


def test_audit_chain_still_verifies_after_migration(flat_home: Path) -> None:
    """Lose the operator key and every chain it signed is unverifiable.

    This is the whole risk of the migration expressed as one assertion: reload
    the key from its NEW location, and verify the chain from ITS new location.
    """
    pubkey, tip_before = _seed_operator_chain(flat_home)

    migrate_arc_home()

    reloaded = OperatorKey.load(path=paths.default_operator_key_path())
    assert reloaded.into_signer().public_key == pubkey

    chain = paths.store_dir() / "audit.jsonl"
    assert chain.exists(), "the signed chain did not arrive at its new home"
    assert verify_chain(chain, pubkey) is True

    tail = json.loads(chain.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert tail["event_hash"] == tip_before, "chain tip changed across the move"


def test_operator_key_keeps_its_owner_only_permissions(flat_home: Path) -> None:
    _seed_operator_chain(flat_home)
    migrate_arc_home()
    assert paths.default_operator_key_path().stat().st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


def test_every_file_is_present_afterwards(flat_home: Path) -> None:
    before = {p.name for p in flat_home.iterdir()}
    migrate_arc_home()

    for name in ("arcagent.toml", "arcllm.toml", "arcrun.toml", "gateway.toml"):
        assert paths.config_file(name).read_text(encoding="utf-8") == f"# {name}\n"
    assert paths.config_file("connections.toml").exists()
    assert paths.env_file().read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=x\n"

    for accessor in ("operator_dir", "identity_dir", "trust_dir", "store_dir", "nats_dir"):
        directory: Path = getattr(paths, accessor)()
        assert (directory / "marker.txt").exists(), f"{accessor} lost its contents"
    assert (paths.bundles_dir() / "marker.txt").exists()
    assert (paths.module_root() / "marker.txt").exists()
    assert paths.users_file().read_text(encoding="utf-8") == "[]"

    moved = {p.name for p in flat_home.iterdir()}
    assert moved <= {"config", "state", "runtime"}
    assert before - moved, "nothing was moved"


def test_state_never_lands_inside_the_replaceable_runtime(flat_home: Path) -> None:
    migrate_arc_home()
    runtime_root = paths.arc_runtime_root()
    for accessor in ("arc_config", "arc_state", "operator_dir", "trust_dir", "store_dir"):
        assert runtime_root not in getattr(paths, accessor)().parents


# --------------------------------------------------------------------------
# The fleet — never moved, by either box, ever
# --------------------------------------------------------------------------


@pytest.fixture
def live_fleet(flat_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fleet where it belongs and already is: outside the home, at ``<tmp>/arc/team``.

    ``ARC_TEAM_ROOT`` reproduces inside the tmp tree the relationship ``~/.arc``
    and ``~/arc`` have on a real box. It has to be set explicitly because
    ``ARC_CONFIG_DIR`` — which ``flat_home`` sets — deliberately carries the
    fleet into isolation with it, and a test that let the default resolve to the
    developer's real ``~/arc/team`` would be reading a live fleet.
    """
    monkeypatch.setenv("ARC_TEAM_ROOT", str(flat_home.parent / "arc"))
    fleet = flat_home.parent / "arc" / "team" / "josh_agent"
    (fleet / "memory").mkdir(parents=True)
    (fleet / "arcagent.toml").write_text('[identity]\ndid = "did:arc:josh"\n', encoding="utf-8")
    (fleet / "memory" / "episodes.jsonl").write_text('{"text":"remember me"}\n', encoding="utf-8")
    return fleet.parent


def test_the_migration_never_moves_the_fleet(live_fleet: Path) -> None:
    """Both live boxes already have their fleet where it belongs. Leave it alone.

    This is the safest property the migration has: the only irreplaceable things
    on a box are ``state/`` and the fleet, and neither is renamed. Splitting the
    home is a matter of moving Arc's OWN files; an agent's memory, identity,
    tools, skills and workspace are never in the blast radius at all.
    """
    before = sorted(p.relative_to(live_fleet) for p in live_fleet.rglob("*"))

    result = migrate_arc_home()

    assert all(dst != paths.arc_team() for _, dst in result.moved)
    assert sorted(p.relative_to(live_fleet) for p in live_fleet.rglob("*")) == before
    assert (live_fleet / "josh_agent" / "memory" / "episodes.jsonl").read_text(
        encoding="utf-8"
    ) == '{"text":"remember me"}\n'


def test_the_fleet_is_still_where_the_resolver_says_afterwards(live_fleet: Path) -> None:
    """Nothing the migration does may change the answer ``arc_team()`` gives."""
    resolved_before = paths.arc_team()

    migrate_arc_home()

    assert paths.arc_team() == resolved_before == live_fleet
    assert (paths.arc_team() / "josh_agent" / "arcagent.toml").exists()


def test_splitting_the_home_leaves_no_agent_data_under_a_disposable_root(
    live_fleet: Path,
) -> None:
    """After the split, nothing an update replaces is an ancestor of the fleet."""
    migrate_arc_home()

    assert paths.arc_runtime_root() not in paths.arc_team().parents
    assert paths.arc_home() not in paths.arc_team().parents


def test_migrating_twice_still_leaves_the_fleet_untouched(live_fleet: Path) -> None:
    """The same procedure runs on two live boxes and gets re-run after a failure."""
    migrate_arc_home()
    second = migrate_arc_home()

    assert second.already_migrated is True
    assert (live_fleet / "josh_agent" / "arcagent.toml").exists()


def test_unknown_entries_are_left_alone(flat_home: Path) -> None:
    """Migration moves what it knows. It never guesses, and never deletes."""
    (flat_home / "team").mkdir()
    (flat_home / "team" / "coder").mkdir()
    (flat_home / "notes.txt").write_text("mine", encoding="utf-8")

    migrate_arc_home()

    assert (flat_home / "team" / "coder").is_dir()
    assert (flat_home / "notes.txt").read_text(encoding="utf-8") == "mine"


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_running_twice_is_a_no_op(flat_home: Path) -> None:
    pubkey, _ = _seed_operator_chain(flat_home)
    first = migrate_arc_home()
    assert first.moved, "first run should have moved something"

    second = migrate_arc_home()
    assert second.moved == []
    assert second.already_migrated is True
    assert verify_chain(paths.store_dir() / "audit.jsonl", pubkey) is True


def test_a_home_that_does_not_exist_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "never-created"))
    result = migrate_arc_home()
    assert result.moved == []
    assert result.already_migrated is True


def test_an_already_split_home_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "arc-home"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root))
    (root / "config").mkdir(parents=True)
    (root / "state" / "operator").mkdir(parents=True)
    (root / "config" / "arcagent.toml").write_text("x", encoding="utf-8")

    result = migrate_arc_home()

    assert result.moved == []
    assert (root / "config" / "arcagent.toml").read_text(encoding="utf-8") == "x"


# --------------------------------------------------------------------------
# Failure leaves a working box
# --------------------------------------------------------------------------


def test_a_failure_mid_move_rolls_everything_back(
    flat_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-migrated home that cannot find its signing key is worse than none.

    The key survives *and is still usable* — reloaded from the FLAT path, still
    able to verify the chain that was never moved.
    """
    pubkey, _ = _seed_operator_chain(flat_home)
    real_replace = paths.__dict__.get("_unused")  # keep ruff from flagging the import
    assert real_replace is None

    import arctrust.home_migration as mod

    original = mod.os.replace
    calls = {"n": 0}

    def exploding_replace(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("simulated failure mid-move")
        original(src, dst)

    monkeypatch.setattr(mod.os, "replace", exploding_replace)

    with pytest.raises(MigrationError):
        migrate_arc_home()

    # Every source is back where it started.
    assert (flat_home / "operator" / "operator.key").exists()
    assert (flat_home / "store" / "audit.jsonl").exists()
    assert (flat_home / "arcagent.toml").exists()

    # And the deployment still works: key loads, chain verifies.
    key = OperatorKey.load(path=flat_home / "operator" / "operator.key")
    assert key.into_signer().public_key == pubkey
    assert verify_chain(flat_home / "store" / "audit.jsonl", pubkey) is True


def test_a_collision_at_the_destination_aborts_before_moving_anything(flat_home: Path) -> None:
    """Never overwrite. If a destination is already occupied, stop and say so."""
    _seed_operator_chain(flat_home)
    (flat_home / "state" / "operator").mkdir(parents=True)
    (flat_home / "state" / "operator" / "operator.key").write_text("IMPOSTOR", encoding="utf-8")

    with pytest.raises(MigrationError, match="already exists"):
        migrate_arc_home()

    assert (flat_home / "operator" / "operator.key").exists()
    assert (flat_home / "arcagent.toml").exists()
    assert (flat_home / "state" / "operator" / "operator.key").read_text(
        encoding="utf-8"
    ) == "IMPOSTOR"


def test_migration_never_deletes_a_source_it_did_not_move(flat_home: Path) -> None:
    """Move semantics only — no copy-then-delete anywhere in the implementation."""
    source = Path(__import__("arctrust.home_migration", fromlist=["x"]).__file__ or "")
    text = source.read_text(encoding="utf-8")
    for forbidden in ("shutil.rmtree", "os.unlink", "Path.unlink", ".unlink(", "os.remove"):
        assert forbidden not in text, f"migration must not delete: found {forbidden}"


def test_an_empty_directory_at_the_destination_is_not_a_collision(flat_home: Path) -> None:
    """Live boxes have these: bare ``mkdir -p`` calls an earlier deploy script left.

    An empty directory holds nothing an operator could lose, so refusing on it
    would abort the migration on exactly the deployments it exists for — and the
    move itself is fine, because ``rename`` replaces an empty directory
    atomically. Occupied means "has contents", not "the path resolves".
    """
    pubkey, _ = _seed_operator_chain(flat_home)
    paths.operator_dir().mkdir(parents=True)
    paths.store_dir().mkdir(parents=True)

    migrate_arc_home()

    assert paths.default_operator_key_path().exists()
    assert verify_chain(paths.store_dir() / "audit.jsonl", pubkey) is True


def test_an_empty_destination_is_only_free_when_both_sides_are_directories(
    flat_home: Path,
) -> None:
    """``rename`` refuses a file onto a directory, so that case must stay a collision.

    Reporting it up front is the difference between a named refusal and an
    ``EISDIR`` part-way through, with half the home already moved.
    """
    paths.env_file().mkdir(parents=True)

    with pytest.raises(MigrationError, match="already exists"):
        migrate_arc_home()

    assert (flat_home / "arc.env").exists()
    assert (flat_home / "operator").is_dir(), "an aborted migration moved something"
