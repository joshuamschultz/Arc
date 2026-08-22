"""``arc workflow`` — SPEC-061 COMP-019 operator CLI.

These tests drive the REAL arcteam engine. There is no injected control plane
and no faked canonicalizer, because faking those seams is exactly what let a
wrong module name (``arcteam.workflows``, plural) survive two rounds of fixes
while every test passed and the entire command group was dead in every build.

The rule this file enforces: nothing that resolves an arcteam symbol may be
monkeypatched. ``_resolve_control_plane`` and ``_resolve_bundle_signer`` are
driven live, ``sign`` writes a sidecar that is re-verified end to end against
the pinned operator key, and a source scan asserts the plural name is gone.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from arctrust.paths import config_file, default_operator_key_path, workflows_dir

from arccli.commands import workflow as wf_cmd
from arccli.commands.workflow import workflow_handler

_DEFINITION = """
[workflow]
id = "{wid}"
version = 1
owner = "@sales"

[[node]]
id = "greet"
kind = "agent"
agent = "@sales"
"""

_SKIPPED_DEFINITION = """
[workflow]
id = "skipped"
version = 1
owner = "@sales"

[[node]]
id = "never-runs"
kind = "agent"
agent = "@sales"
when = "false"
"""


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every entry point resolves its data root from these two env vars."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    """The config dir with a bootstrapped operator key, as first run leaves it."""
    from arccli.commands.operator import load_operator_key

    load_operator_key(tmp_path)
    return tmp_path


def _write_bundle(root: Path, wid: str = "onboarding") -> Path:
    bundle = root / wid
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(_DEFINITION.format(wid=wid), encoding="utf-8")
    return bundle


def _write_skipped_bundle(root: Path) -> Path:
    bundle = root / "skipped"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(_SKIPPED_DEFINITION, encoding="utf-8")
    return bundle


# ---------------------------------------------------------------------------
# The seams resolve real arcteam objects — no monkeypatching anywhere here
# ---------------------------------------------------------------------------


def test_resolve_control_plane_returns_the_real_arcteam_control_plane(arc_dir: Path) -> None:
    """The defect this file exists for: the group was dead at the import seam.

    Drives the resolver live. A wrong module path, a missing factory, or a
    constructor whose arguments do not match arcteam's real signature all fail
    here — which is the only place they can fail before an operator hits them.
    """
    import asyncio

    from arcteam.workflow.control_plane import WorkflowControlPlane

    async def _drive() -> object:
        plane, aclose = await wf_cmd._resolve_control_plane(arc_dir, tier="personal")
        try:
            return plane
        finally:
            await aclose()

    assert isinstance(asyncio.run(_drive()), WorkflowControlPlane)


def test_resolve_bundle_signer_returns_the_real_definition_store(arc_dir: Path) -> None:
    """``_resolve_bundle_signer`` must hand back arcteam's own COMP-005 store."""
    from arcteam.workflow import DefinitionStore

    store = wf_cmd._resolve_bundle_signer(workflows_dir(arc_dir), arc_dir)

    assert isinstance(store, DefinitionStore)
    assert store.root == workflows_dir(arc_dir)


def test_module_never_names_the_plural_arcteam_workflows_package() -> None:
    """``arcteam.workflows`` does not exist; ``arcteam.workflow`` does.

    The plural survived two fix rounds because it lived inside an
    ``importlib.import_module`` string that no linter or type checker reads.
    """
    source = Path(wf_cmd.__file__).read_text(encoding="utf-8")

    assert "arcteam.workflows" not in source
    assert "importlib" not in source


# ---------------------------------------------------------------------------
# sign / verify — real signing rail, REQ-224 (key never enters the plane)
# ---------------------------------------------------------------------------


def test_sign_writes_arcsig_that_verifies_against_pinned_operator_key(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end on a real bundle: sign, then prove the sidecar verifies.

    ``arc workflow sign`` is the ONLY path to signed status, so at enterprise
    or federal tier a broken signer means no workflow can ever run.
    """
    bundle = _write_bundle(workflows_dir(arc_dir))

    workflow_handler(["sign", str(bundle), "--dir", str(arc_dir)])

    assert (bundle / "workflow.toml.arcsig").is_file()
    assert "Signed workflow.toml" in capsys.readouterr().out

    workflow_handler(["verify", str(bundle), "--dir", str(arc_dir)])
    out = capsys.readouterr().out
    assert out.startswith("VALID")
    assert "operator:" in out


def test_signed_bundle_reads_as_signed_through_the_store(arc_dir: Path) -> None:
    """Trust must be visible to the engine, not only to the CLI's own printout."""
    bundle = _write_bundle(workflows_dir(arc_dir))
    workflow_handler(["sign", str(bundle), "--dir", str(arc_dir)])

    store = wf_cmd._resolve_bundle_signer(workflows_dir(arc_dir), arc_dir)
    loaded = store.load("onboarding")

    assert loaded.is_verified is True
    assert loaded.status == "signed"


def test_verify_refuses_a_bundle_signed_by_a_foreign_key(arc_dir: Path) -> None:
    """Pinned, not TOFU: a self-signed definition is refused (SPEC-047 HIGH-1)."""
    from arcteam.workflow import DefinitionStore, sign_definition
    from arctrust import OperatorKey

    root = workflows_dir(arc_dir)
    _write_bundle(root)
    attacker = OperatorKey.load(arc_dir / "attacker.key", generate_if_absent=True)
    sign_definition(
        DefinitionStore(root),
        "onboarding",
        signer_did="operator:attacker",
        private_key=attacker.seed,
    )

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["verify", str(root / "onboarding"), "--dir", str(arc_dir)])

    assert exc.value.code == 1


def test_verify_reports_invalid_for_unsigned_bundle(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _write_bundle(workflows_dir(arc_dir))

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["verify", str(bundle), "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert "INVALID" in capsys.readouterr().out


def test_sign_missing_workflow_toml_errors(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        workflow_handler(["sign", str(arc_dir / "does-not-exist")])

    assert "workflow.toml not found" in capsys.readouterr().err


def test_sign_never_resolves_the_control_plane(
    arc_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-224: the signing key is resolved only in the CLI process.

    The one monkeypatch in this file, and it patches the seam to EXPLODE
    rather than to succeed — it can only make a test fail, never pass.
    """

    def _must_not_be_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("arc workflow sign must never resolve the control plane")

    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", _must_not_be_called)
    bundle = _write_bundle(workflows_dir(arc_dir))

    workflow_handler(["sign", str(bundle), "--dir", str(arc_dir)])

    assert (bundle / "workflow.toml.arcsig").is_file()


# ---------------------------------------------------------------------------
# Reads — the real definition store
# ---------------------------------------------------------------------------


def test_list_reports_no_workflows(arc_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workflow_handler(["list", "--dir", str(arc_dir)])

    assert "No workflows." in capsys.readouterr().out


def test_list_renders_real_bundles_and_hides_archived(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = workflows_dir(arc_dir)
    _write_bundle(root, "onboarding")
    _write_bundle(root, "renewal")
    wf_cmd._resolve_bundle_signer(root, arc_dir).archive("renewal", actor_did="did:arc:test")

    workflow_handler(["list", "--dir", str(arc_dir)])
    visible = capsys.readouterr().out
    workflow_handler(["list", "--all", "--dir", str(arc_dir)])
    everything = capsys.readouterr().out

    assert "onboarding" in visible
    assert "renewal" not in visible
    assert "renewal" in everything


def test_show_prints_the_real_definition_and_trust(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    _write_bundle(workflows_dir(arc_dir))

    workflow_handler(["show", "onboarding", "--dir", str(arc_dir)])

    detail = json.loads(capsys.readouterr().out)
    assert detail["id"] == "onboarding"
    assert detail["status"] == "draft"
    assert detail["is_verified"] is False
    assert detail["definition"]["node"][0]["id"] == "greet"


def test_show_unknown_workflow_errors_cleanly(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        workflow_handler(["show", "nope", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert "Error:" in capsys.readouterr().err


def test_show_refuses_a_traversing_workflow_id(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An id becomes a directory under the workspace; it must stay a bare name."""
    with pytest.raises(SystemExit) as exc:
        workflow_handler(["show", "../../etc", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert "Error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Mutations and initiation — the real WorkflowControlPlane
# ---------------------------------------------------------------------------


def test_create_writes_a_draft_through_the_real_control_plane(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_bundle(tmp_path / "src")

    workflow_handler(["create", str(source), "--dir", str(arc_dir)])

    assert "Created draft workflow onboarding v1 (status=draft)" in capsys.readouterr().out
    assert (workflows_dir(arc_dir) / "onboarding" / "workflow.toml").is_file()


def test_create_relays_typed_validation_errors_and_exits_nonzero(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Authoring never confers trust, and a bad graph is refused with repair lines."""
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "workflow.toml").write_text(
        '[workflow]\nid = "broken"\nversion = 1\n\n'
        '[[node]]\nid = "a"\nkind = "agent"\nagent = "@x"\nneeds = ["ghost"]\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["create", str(bad), "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert capsys.readouterr().err.strip() != ""


def test_edit_bumps_the_version_and_refuses_a_stale_expected_version(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_bundle(tmp_path / "src")
    workflow_handler(["create", str(source), "--dir", str(arc_dir)])
    capsys.readouterr()

    (source / "workflow.toml").write_text(
        _DEFINITION.format(wid="onboarding").replace('"@sales"', '"@support"'), encoding="utf-8"
    )

    workflow_handler(
        [
            "edit",
            "onboarding",
            "--document",
            str(source),
            "--expected-version",
            "1",
            "--reason",
            "warmer",
            "--dir",
            str(arc_dir),
        ]
    )
    assert "Edited onboarding -> v2 (status=draft)" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        workflow_handler(
            [
                "edit",
                "onboarding",
                "--document",
                str(source),
                "--expected-version",
                "1",
                "--dir",
                str(arc_dir),
            ]
        )
    assert exc.value.code == 1


def test_archive_unarchive_round_trip_through_the_plane(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(["create", str(_write_bundle(tmp_path / "src")), "--dir", str(arc_dir)])
    capsys.readouterr()

    workflow_handler(["archive", "onboarding", "--dir", str(arc_dir)])
    assert (workflows_dir(arc_dir) / "onboarding" / "archived.json").is_file()

    workflow_handler(["unarchive", "onboarding", "--dir", str(arc_dir)])
    out = capsys.readouterr().out
    assert "Archived onboarding." in out
    assert "Unarchived onboarding (status=draft)." in out
    assert not (workflows_dir(arc_dir) / "onboarding" / "archived.json").exists()


def test_purge_destroys_a_workflow_with_no_runs(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_handler(["create", str(_write_bundle(tmp_path / "src")), "--dir", str(arc_dir)])
    capsys.readouterr()

    workflow_handler(["purge", "onboarding", "--dir", str(arc_dir)])

    assert "Purged onboarding." in capsys.readouterr().out
    assert not (workflows_dir(arc_dir) / "onboarding").exists()


def test_purge_refuses_while_a_real_run_references_the_workflow(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The orphan guard must count REAL runs from the run store.

    A stub returning zero leaves this guard present and inert — the exact
    failure shape this feature produced repeatedly — so the run is created
    through ``arcstore.runs.RunStore``, which is what the control plane counts.
    """
    import asyncio

    from arcstore import store_db_path
    from arcstore.backends.sqlite import SqliteBackend
    from arcstore.runs import Run, RunStore

    workflow_handler(["create", str(_write_bundle(tmp_path / "src")), "--dir", str(arc_dir)])
    capsys.readouterr()

    async def _seed_run() -> None:
        backend = SqliteBackend(store_db_path(None))
        await backend.start()
        await RunStore(backend).create(
            Run(
                id="run-1",
                workflow_id="onboarding",
                workflow_version=1,
                content_hash="0" * 64,
                status="running",
                initiator_did="did:arc:local:operator/test",
            )
        )
        await backend.stop()

    asyncio.run(_seed_run())

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["purge", "onboarding", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert "refusing to purge" in capsys.readouterr().err
    assert (workflows_dir(arc_dir) / "onboarding").is_dir()

    workflow_handler(["purge", "onboarding", "--force", "--dir", str(arc_dir)])
    assert not (workflows_dir(arc_dir) / "onboarding").exists()


def test_run_refuses_an_unknown_workflow_cleanly(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        workflow_handler(["run", "nope", "--dir", str(arc_dir)])

    assert exc.value.code == 1
    assert capsys.readouterr().err.strip() != ""


def test_attached_cli_run_owns_a_headless_runner_through_terminal_state(
    arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A terminal workflow must complete without an ArcUI or gateway process."""
    _write_skipped_bundle(workflows_dir(arc_dir))

    workflow_handler(["run", "skipped", "--interval", "0.01", "--dir", str(arc_dir)])

    output = capsys.readouterr().out
    assert "Started run" in output
    assert "finished (status=done)" in output


def test_signed_definition_round_trips_from_create_through_sign(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator's whole path: author a draft, then sign what landed.

    Signing must cover the bundle the CONTROL PLANE wrote, at the root the
    runner reads — a CLI signing a different directory would leave every
    definition unsigned to the engine.
    """
    workflow_handler(["create", str(_write_bundle(tmp_path / "src")), "--dir", str(arc_dir)])
    landed = workflows_dir(arc_dir) / "onboarding"
    capsys.readouterr()

    workflow_handler(["sign", str(landed), "--dir", str(arc_dir)])
    workflow_handler(["show", "onboarding", "--dir", str(arc_dir)])

    capsys.readouterr()
    workflow_handler(["list", "--dir", str(arc_dir)])
    assert "signed" in capsys.readouterr().out


def test_no_dir_flag_lands_where_the_runner_reads(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--dir`` the CLI must use the deployment's config dir.

    Caught by running the real CLI: ``_arc_dir`` hardcoded ``~/.arc`` and
    ignored ``ARC_CONFIG_DIR``, so ``create`` wrote a bundle the operator then
    could not sign at the path they had just been shown — and on a deployment
    running under ``ARC_CONFIG_DIR`` the runner would have read a directory the
    CLI never wrote to. Both halves look healthy; nothing is ever signed.
    """
    from arcteam.workflow.runner import build_workflow_runner

    workflow_handler(["create", str(_write_bundle(tmp_path / "src"))])

    assert (workflows_dir(arc_dir) / "onboarding" / "workflow.toml").is_file()
    capsys.readouterr()

    # The root build_workflow_runner derives from its key path, unchanged.
    from arccli.commands.operator import operator_key_path

    assert operator_key_path(arc_dir) == default_operator_key_path(arc_dir)
    assert build_workflow_runner is not None

    workflow_handler(["sign", str(workflows_dir(arc_dir) / "onboarding")])
    workflow_handler(["verify", str(workflows_dir(arc_dir) / "onboarding")])
    assert "VALID" in capsys.readouterr().out


def test_first_run_on_a_fresh_config_dir_needs_no_setup_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Zero-config: ``create`` must work on a box with no operator key yet.

    Deliberately does NOT use the ``arc_dir`` fixture — that fixture bootstraps
    the key, and it was hiding this: the control plane was built before the
    actor was resolved, so the runner's fail-closed identity load refused
    ("No operator key ...") and every mutation was unreachable until the
    operator ran some other command first.
    """
    assert not default_operator_key_path(tmp_path).exists()

    workflow_handler(["create", str(_write_bundle(tmp_path / "src"))])

    assert "Created draft workflow onboarding" in capsys.readouterr().out
    assert default_operator_key_path(tmp_path).is_file()


def test_editing_a_signed_bundle_drops_the_signature(
    arc_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Authoring can never confer trust: a revision returns to draft (REQ-223)."""
    source = _write_bundle(tmp_path / "src")
    workflow_handler(["create", str(source), "--dir", str(arc_dir)])
    workflow_handler(["sign", str(workflows_dir(arc_dir) / "onboarding"), "--dir", str(arc_dir)])
    capsys.readouterr()

    (source / "workflow.toml").write_text(
        _DEFINITION.format(wid="onboarding").replace('"@sales"', '"@support"'), encoding="utf-8"
    )
    workflow_handler(
        [
            "edit",
            "onboarding",
            "--document",
            str(source),
            "--expected-version",
            "1",
            "--dir",
            str(arc_dir),
        ]
    )

    assert "status=draft" in capsys.readouterr().out
    assert not (workflows_dir(arc_dir) / "onboarding" / "workflow.toml.arcsig").exists()


# ---------------------------------------------------------------------------
# Removability (REQ-257/REQ-230 contribution from this package)
# ---------------------------------------------------------------------------


def test_workflow_module_has_no_arcui_dependency() -> None:
    """The CLI's workflow surface must not depend on the dashboard package.

    Static source scan (not sys.modules — arcui may already be imported by an
    unrelated test in the same pytest process) proves `arc workflow` works
    whether or not arcui is installed.
    """
    src = Path(wf_cmd.__file__).read_text(encoding="utf-8")
    assert "import arcui" not in src
    assert "from arcui" not in src


def test_every_subcommand_is_reachable() -> None:
    """Deleting the dashboard must never remove a capability reachable here."""
    assert set(wf_cmd._SUBCOMMAND_MAP) == {
        "list",
        "show",
        "create",
        "edit",
        "archive",
        "unarchive",
        "purge",
        "run",
        "serve",
        "cancel",
        "sign",
        "verify",
    }


def test_workflow_handler_empty_args_prints_help_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        workflow_handler([])

    assert exc.value.code == 0
    assert "workflow" in capsys.readouterr().out.lower()


def test_deployment_tier_is_read_from_config_not_guessed(arc_dir: Path) -> None:
    """Tier flows from construction; a stringency dial the CLI must not invent."""
    assert wf_cmd._deployment_tier(arc_dir) == "personal"
    machine_config = config_file("arcagent.toml", arc_dir)
    machine_config.parent.mkdir(parents=True, exist_ok=True)
    machine_config.write_text('[security]\ntier = "federal"\n', encoding="utf-8")
    assert wf_cmd._deployment_tier(arc_dir) == "federal"
    assert tomllib is not None
