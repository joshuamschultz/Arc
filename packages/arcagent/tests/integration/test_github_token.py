"""GitHub through `gh` with a vaulted fine-grained token placed as GH_TOKEN (P18-3, 3.H).

The SHIPPED ``github`` bundle, a real ``gh``-shaped executable on PATH (a script that
reports which environment variables it was given and prints GitHub's token-expiration
header), real sealed custody and the real health probe. Nothing is faked above the
spawned process: the token travels the same handle -> spawn path production uses.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import make_cipher

from arcagent.connections import AuditChain, Connections
from arcagent.core.tier import Tier
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import credential_plan
from arcagent.extension.custody_select import Custody, open_custody
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.manifest import load_manifest
from arcagent.extension.state import open_connection_state
from arcagent.modules.connectors.install import install_connector, plan_connector

_REPO = Path(__file__).resolve().parents[4]
_INSTANCE = "work"
_CALLER = "did:arc:testorg:executor/github"
_TOKEN = "github_pat_11AAAA_secretsecretsecret"

#: A `gh` stand-in. It answers --version, and for `api --include user` it prints the
#: response headers GitHub sends (with the expiry from $FAKE_GH_EXPIRES) and a JSON
#: body, then reports which variables it received. It deliberately PRINTS the token
#: it was given, to prove Arc's redaction keeps it out of every result.
_FAKE_GH = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "gh version 2.97.0 (fake)"; exit 0; fi
printf 'HTTP/2.0 200 OK\\n'
printf 'X-Oauth-Scopes: \\n'
if [ -n "$FAKE_GH_EXPIRES" ]; then
  printf 'GitHub-Authentication-Token-Expiration: %s\\n' "$FAKE_GH_EXPIRES"
fi
printf '\\n{"login":"octo","token_seen":"%s","config_dir":"%s","config_entries":"%s"}\\n' \\
  "$GH_TOKEN" "$GH_CONFIG_DIR" "$(ls -A "$GH_CONFIG_DIR" | wc -l | tr -d ' ')"
"""


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


async def _opener(backend: FakeBackend) -> FakeBackend:
    return backend


def _custody(backend: FakeBackend) -> Custody:
    return open_custody(
        backend, make_cipher(), health=StoreHealthReporter(lambda: _opener(backend))
    )


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(_FAKE_GH)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # An operator who has run `gh auth login`: a real config the child must never see.
    home = tmp_path / "home"
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text(
        "github.com:\n  oauth_token: operator-own\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return bin_dir


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.backend = FakeBackend()
        self.root = tmp_path / "extensions"
        self.root.mkdir()
        shutil.copytree(
            _REPO / "extensions" / "github",
            self.root / "github",
            ignore=shutil.ignore_patterns("__pycache__", "tests"),
        )
        self.arc_dir = tmp_path / "arc"
        self.arc_dir.mkdir()

    def connections(self) -> Connections:
        return Connections.for_deployment(
            arc_dir=self.arc_dir,
            data_dir=self.tmp_path / "data",
            extensions_root=self.root,
            audit=AuditChain.held(_Sink()),
            state_opener=lambda: _opener(self.backend),
            credential_cipher=make_cipher(),
            oauth_redirect_uri="http://127.0.0.1:8420/oauth/callback",
        )

    async def install(self) -> None:
        custody = _custody(self.backend)
        plan = plan_connector(
            extensions_root=[self.root],
            extension="github",
            instance=_INSTANCE,
            tier=Tier.PERSONAL,
            audit_sink=_Sink(),
        )
        broker = custody.broker(registry=lambda: ConnectionRegistry(self.arc_dir))
        await install_connector(
            plan,
            connections=ConnectionRegistry(self.arc_dir),
            agents=["reader"],
            secret_values={"token": _TOKEN},
            store=custody.store,
            caller_did=_CALLER,
            state=await open_connection_state(opener=lambda: _opener(self.backend)),
            credential=broker.operator_handle(
                _INSTANCE, actor_did=_CALLER, plan=credential_plan(plan.manifest)
            ),
        )


@pytest.fixture
async def world(tmp_path: Path, fake_gh: Path) -> _World:
    built = _World(tmp_path)
    await built.install()
    return built


def _files_containing(root: Path, needle: str) -> list[Path]:
    hits: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                if needle.encode() in path.read_bytes():
                    hits.append(path)
            except OSError:
                continue
    return hits


def _expires(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S UTC")


async def test_healthy_token_records_its_expiry_and_stays_healthy(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_EXPIRES", _expires(90))

    record = await world.connections().check_health(_INSTANCE, checked_by=_CALLER)

    assert record.status == "healthy"
    assert record.credential_expires_at is not None, "the expiry header is mirrored for display"


async def test_a_token_within_seven_days_of_expiry_is_needs_you(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_EXPIRES", _expires(3.5))

    record = await world.connections().check_health(_INSTANCE, checked_by=_CALLER)

    assert record.status == "needs_you"
    assert record.reason_code == "token_expiring"
    assert record.reason_text is not None and "3 days" in record.reason_text
    assert record.credential_expires_at is not None


async def test_a_token_with_no_expiry_header_passes(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKE_GH_EXPIRES", raising=False)

    record = await world.connections().check_health(_INSTANCE, checked_by=_CALLER)

    assert record.status == "healthy"


async def test_token_reaches_gh_only_as_GH_TOKEN_per_spawn_and_is_redacted_everywhere(  # noqa: N802
    world: _World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_EXPIRES", _expires(90))
    caplog.set_level(logging.DEBUG)

    record = await world.connections().check_health(_INSTANCE, checked_by=_CALLER)

    assert record.status == "healthy"
    # The fake gh PRINTED the token it received, so a probe that passed proves the
    # placement worked; the same text must be scrubbed from every place it could land.
    assert _TOKEN not in caplog.text
    assert _TOKEN not in (record.reason_text or "")
    assert _TOKEN not in record.model_dump_json()
    assert _files_containing(world.tmp_path / "arc", _TOKEN) == []
    assert _files_containing(world.tmp_path / "data", _TOKEN) == []
    for path in world.root.rglob("*"):
        assert not path.is_file() or _TOKEN.encode() not in path.read_bytes(), path


async def test_the_token_is_never_written_to_an_env_file_on_disk(world: _World) -> None:
    # The only sealed copy is custody's; no plaintext file anywhere in the deployment.
    leaked = _files_containing(world.tmp_path, _TOKEN)
    assert leaked == [], f"plaintext token found in {leaked}"


async def test_gh_never_reads_the_operator_gh_config(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH_CONFIG_DIR is an empty directory of this connection's own, not ~/.config/gh."""
    monkeypatch.setenv("FAKE_GH_EXPIRES", _expires(90))
    connections = world.connections()
    with connections._audit.open() as sink:  # reach the attachment the probe uses
        plan = connections._plan_for(_INSTANCE, sink)
        attachment = await connections._attachment(plan, sink)
    result = await attachment.invoke("github_whoami", {})

    assert result.content.count('"config_entries":"0"') == 1, "the config directory is empty"
    operator_config = os.environ["HOME"] + "/.config/gh"
    assert operator_config not in result.content
    assert "operator-own" not in result.content
    assert _TOKEN not in result.content, "the token the child printed was redacted"


def test_static_env_cannot_override_GH_TOKEN_or_PATH() -> None:  # noqa: N802
    base = (_REPO / "extensions" / "github" / "extension.toml").read_text()
    for name in ("GH_TOKEN", "PATH", "LD_PRELOAD", "GITHUB_TOKEN", "TOKEN", "gh_prompt"):
        hostile = base.replace(
            'static_env = { GH_PROMPT_DISABLED = "1"',
            f'static_env = {{ {name} = "x", GH_PROMPT_DISABLED = "1"',
        )
        assert hostile != base
        with pytest.raises(ValueError, match="static_env|may not set|not valid|environment name"):
            load_manifest(hostile, tier=Tier.PERSONAL)


def test_isolated_config_env_cannot_name_the_credential_variable() -> None:
    base = (_REPO / "extensions" / "github" / "extension.toml").read_text()
    hostile = base.replace(
        'isolated_config_env = "GH_CONFIG_DIR"', 'isolated_config_env = "GH_TOKEN"'
    )
    with pytest.raises(ValueError, match="may not set"):
        load_manifest(hostile, tier=Tier.PERSONAL)
