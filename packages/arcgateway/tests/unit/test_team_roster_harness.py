"""H-040 §4 — the roster is harness-aware: native badge + foreign members surface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from arcgateway import team_roster


def _write_agent(root: Path, name: str, *, harness: str | None = None) -> None:
    d = root / name
    d.mkdir(parents=True)
    lines = ["[agent]", f'name = "{name}"', 'type = "executor"']
    if harness is not None:
        lines.append(f'harness = "{harness}"')
    lines += ["", "[identity]", f'did = "did:arc:test:executor/{name}"']
    (d / "arcagent.toml").write_text("\n".join(lines), encoding="utf-8")


def test_disk_agent_defaults_to_arcagent_harness(tmp_path: Path) -> None:
    _write_agent(tmp_path, "alice")
    [entry] = team_roster.list_team(team_root=tmp_path, online_ids=set())
    assert entry.harness == "arcagent"


def test_disk_agent_can_declare_a_foreign_harness(tmp_path: Path) -> None:
    _write_agent(tmp_path, "bob", harness="hermes")
    [entry] = team_roster.list_team(team_root=tmp_path, online_ids=set())
    assert entry.harness == "hermes"


@dataclass
class _Status:
    value: str


@dataclass
class _Entity:
    did: str
    handle: str
    name: str
    harness: str
    status: _Status
    type: _Status  # duck-typed .value


def _foreign(handle: str, *, harness: str = "hermes", status: str = "active") -> _Entity:
    return _Entity(
        did=f"did:arc:acme:{harness}/{handle}",
        handle=handle,
        name=handle.title(),
        harness=harness,
        status=_Status(status),
        type=_Status("agent"),
    )


def test_merge_foreign_members_adds_a_foreign_registry_member() -> None:
    merged = team_roster.merge_foreign_members([], [_foreign("hermes")], online_ids={"hermes"})
    [row] = merged
    assert row.harness == "hermes"
    assert row.did == "did:arc:acme:hermes/hermes"
    assert row.online is True


def test_merge_foreign_members_skips_native_and_inactive() -> None:
    entities = [
        _foreign("nat", harness="arcagent"),  # native → comes from disk, not here
        _foreign("revoked_one", status="revoked"),  # not active
        _foreign("live"),
    ]
    merged = team_roster.merge_foreign_members([], entities, online_ids=set())
    assert [r.agent_id for r in merged] == ["live"]


def test_merge_foreign_members_does_not_double_list_a_known_did(tmp_path: Path) -> None:
    _write_agent(tmp_path, "shared", harness="hermes")
    base = team_roster.list_team(team_root=tmp_path, online_ids=set())
    dup = _foreign("shared")
    dup.did = base[0].did  # same DID already on disk
    merged = team_roster.merge_foreign_members(base, [dup], online_ids=set())
    assert len(merged) == 1
