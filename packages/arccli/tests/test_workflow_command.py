"""``arc workflow`` — SPEC-061 COMP-019 operator CLI.

Non-signing subcommands delegate to a WorkflowControlPlane (COMP-021); since
arcteam's real implementation is being built concurrently on a sibling
branch, these tests inject fakes implementing the Protocols defined in
``arccli.commands.workflow`` — proving the CLI's argument parsing, actor-DID
resolution, and output formatting independent of whether arcteam's engine has
landed in this checkout yet.

``sign``/``verify`` are exercised against the REAL signing rail
(``arctrust.artifact`` + ``arcagent.capabilities.artifact_signing``) with only
the bundle-canonicalization step faked — proving the operator-key handling
(never entering the control plane, REQ-224) end to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arccli.commands import workflow as wf_cmd
from arccli.commands.workflow import WorkflowVerifyResult, workflow_handler

_AGENT_DID = "did:arc:local:operator/deadbeef"


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


class _FakeControlPlane:
    """Records every call it receives; returns canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_workflows(
        self, *, actor_did: str, include_archived: bool
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("list_workflows", {"actor_did": actor_did, "include_archived": include_archived})
        )
        if include_archived:
            return [{"id": "onboarding", "version": 3, "status": "signed", "trigger": "cron"}]
        return []

    async def show(
        self, workflow_id: str, *, actor_did: str, version: int | None
    ) -> dict[str, Any]:
        self.calls.append(
            ("show", {"workflow_id": workflow_id, "actor_did": actor_did, "version": version})
        )
        return {"id": workflow_id, "version": version or 3, "status": "draft"}

    async def create(self, *, definition_path: Path, actor_did: str) -> dict[str, Any]:
        self.calls.append(
            ("create", {"definition_path": definition_path, "actor_did": actor_did})
        )
        return {"id": "onboarding", "version": 1, "status": "draft"}

    async def edit(
        self,
        workflow_id: str,
        *,
        patch_path: Path,
        expected_version: int,
        actor_did: str,
        reason: str,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "edit",
                {
                    "workflow_id": workflow_id,
                    "patch_path": patch_path,
                    "expected_version": expected_version,
                    "actor_did": actor_did,
                    "reason": reason,
                },
            )
        )
        return {"version": expected_version + 1, "status": "draft"}

    async def archive(self, workflow_id: str, *, actor_did: str) -> dict[str, Any]:
        self.calls.append(("archive", {"workflow_id": workflow_id, "actor_did": actor_did}))
        return {"status": "archived"}

    async def unarchive(self, workflow_id: str, *, actor_did: str) -> dict[str, Any]:
        self.calls.append(("unarchive", {"workflow_id": workflow_id, "actor_did": actor_did}))
        return {"status": "draft"}

    async def purge(self, workflow_id: str, *, actor_did: str, force: bool) -> dict[str, Any]:
        self.calls.append(
            ("purge", {"workflow_id": workflow_id, "actor_did": actor_did, "force": force})
        )
        if workflow_id == "has-runs" and not force:
            return {"purged": False, "reason": "runs still reference this workflow"}
        return {"purged": True}

    async def run(
        self, workflow_id: str, *, run_input: dict[str, Any], actor_did: str
    ) -> dict[str, Any]:
        self.calls.append(
            ("run", {"workflow_id": workflow_id, "run_input": run_input, "actor_did": actor_did})
        )
        return {"run_id": "run-1", "status": "running"}

    async def cancel(self, run_id: str, *, actor_did: str) -> dict[str, Any]:
        self.calls.append(("cancel", {"run_id": run_id, "actor_did": actor_did}))
        return {"status": "cancelled"}


class _FakeBundleSigner:
    """Stands in for arcteam COMP-005: canonicalizes + verifies over the real rail."""

    def canonical_bytes(self, bundle_dir: Path) -> bytes:
        return (bundle_dir / "workflow.toml").read_bytes()

    def verify_signature(
        self, bundle_dir: Path, *, trusted_public_key: bytes | None
    ) -> WorkflowVerifyResult:
        from arcagent.capabilities.artifact_signing import load_signature
        from arctrust.artifact import verify_artifact

        toml_path = bundle_dir / "workflow.toml"
        manifest = load_signature(toml_path)
        if manifest is None:
            return WorkflowVerifyResult(valid=False, signer_did=None, detail="unsigned")
        ok = verify_artifact(
            self.canonical_bytes(bundle_dir), manifest, trusted_public_key=trusted_public_key
        )
        return WorkflowVerifyResult(
            valid=ok,
            signer_did=manifest.signer_did if ok else None,
            detail="ok" if ok else "signature mismatch or foreign signer",
        )


def _write_bundle(tmp_path: Path, name: str = "onboarding") -> Path:
    bundle = tmp_path / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(
        '[workflow]\nid = "onboarding"\nversion = 1\n', encoding="utf-8"
    )
    return bundle


# ---------------------------------------------------------------------------
# Non-signing operations — delegate to WorkflowControlPlane (COMP-021)
# ---------------------------------------------------------------------------


def test_list_reports_no_workflows(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)

    workflow_handler(["list"])

    assert "No workflows." in capsys.readouterr().out
    assert fake.calls == [
        ("list_workflows", {"actor_did": fake.calls[0][1]["actor_did"], "include_archived": False})
    ]


def test_list_all_prints_table_and_passes_actor_did(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)

    workflow_handler(["list", "--all"])

    out = capsys.readouterr().out
    assert "onboarding" in out
    name, kwargs = fake.calls[0]
    assert name == "list_workflows"
    assert kwargs["include_archived"] is True
    assert kwargs["actor_did"].startswith("did:arc:")


def test_show_prints_json_detail(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)

    workflow_handler(["show", "onboarding", "--version", "2"])

    out = capsys.readouterr().out
    assert '"id"' in out
    assert '"onboarding"' in out
    assert fake.calls[0] == (
        "show",
        {"workflow_id": "onboarding", "actor_did": fake.calls[0][1]["actor_did"], "version": 2},
    )


def test_create_reports_draft_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)
    bundle = _write_bundle(tmp_path)

    workflow_handler(["create", str(bundle)])

    out = capsys.readouterr().out
    assert "Created draft workflow onboarding v1 (status=draft)" in out
    assert fake.calls[0][0] == "create"
    assert fake.calls[0][1]["definition_path"] == bundle.resolve()


def test_edit_requires_expected_version_and_reports_new_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)
    patch = tmp_path / "patch.json"
    patch.write_text("{}", encoding="utf-8")

    workflow_handler(
        [
            "edit",
            "onboarding",
            "--patch",
            str(patch),
            "--expected-version",
            "3",
            "--reason",
            "widen scope",
        ]
    )

    out = capsys.readouterr().out
    assert "Edited onboarding -> v4 (status=draft)" in out
    _, kwargs = fake.calls[0]
    assert kwargs["expected_version"] == 3
    assert kwargs["reason"] == "widen scope"


def test_archive_unarchive_round_trip(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)

    workflow_handler(["archive", "onboarding"])
    workflow_handler(["unarchive", "onboarding"])

    out = capsys.readouterr().out
    assert "Archived onboarding." in out
    assert "Unarchived onboarding (status=draft)." in out
    assert [c[0] for c in fake.calls] == ["archive", "unarchive"]


def test_purge_refused_reports_reason(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["purge", "has-runs"])

    assert exc.value.code == 1
    assert "runs still reference" in capsys.readouterr().err


def test_purge_force_succeeds(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)

    workflow_handler(["purge", "has-runs", "--force"])

    assert "Purged has-runs." in capsys.readouterr().out
    assert fake.calls[0][1]["force"] is True


def test_run_and_cancel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControlPlane()
    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", lambda: fake)
    input_path = tmp_path / "input.json"
    input_path.write_text('{"customer": "acme"}', encoding="utf-8")

    workflow_handler(["run", "onboarding", "--input", str(input_path)])
    workflow_handler(["cancel", "run-1"])

    out = capsys.readouterr().out
    assert "Started run run-1 for onboarding (status=running)" in out
    assert "Cancelled run-1 (status=cancelled)." in out
    run_call = next(c for c in fake.calls if c[0] == "run")
    assert run_call[1]["run_input"] == {"customer": "acme"}


def test_control_plane_unavailable_reports_clean_error_not_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """arcteam.workflows.control_plane genuinely does not exist yet in this checkout.

    T-869/REQ-257 spirit for this package's half: the CLI degrades to a clean,
    actionable error rather than an unhandled ImportError traceback.
    """
    with pytest.raises(SystemExit) as exc:
        workflow_handler(["list"])

    assert exc.value.code == 1
    assert "WorkflowControlPlane (COMP-021)" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# sign / verify — real signing rail, REQ-224 (key never enters the control plane)
# ---------------------------------------------------------------------------


def test_sign_writes_arcsig_that_verifies_against_pinned_operator_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wf_cmd, "_resolve_bundle_signer", lambda: _FakeBundleSigner())
    bundle = _write_bundle(tmp_path)

    workflow_handler(["sign", str(bundle), "--dir", str(tmp_path)])

    sidecar = bundle / "workflow.toml.arcsig"
    assert sidecar.exists()
    assert "Signed workflow.toml -> workflow.toml.arcsig" in capsys.readouterr().out

    workflow_handler(["verify", str(bundle), "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert out.startswith("VALID")
    assert "operator:" in out


def test_verify_reports_invalid_for_unsigned_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wf_cmd, "_resolve_bundle_signer", lambda: _FakeBundleSigner())
    bundle = _write_bundle(tmp_path)

    with pytest.raises(SystemExit) as exc:
        workflow_handler(["verify", str(bundle), "--dir", str(tmp_path)])

    assert exc.value.code == 1
    assert "INVALID" in capsys.readouterr().out


def test_sign_missing_workflow_toml_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        workflow_handler(["sign", str(tmp_path / "does-not-exist")])

    assert "workflow.toml not found" in capsys.readouterr().err


def test_sign_never_touches_the_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-224: the signing key is resolved only in the CLI process.

    Proven structurally: `_sign` never resolves (or calls) the shared,
    agent-reachable control plane. A control-plane resolver that raises if
    invoked would fail this test if `_sign` ever called it.
    """

    def _must_not_be_called() -> Any:
        raise AssertionError("arc workflow sign must never resolve the control plane")

    monkeypatch.setattr(wf_cmd, "_resolve_control_plane", _must_not_be_called)
    monkeypatch.setattr(wf_cmd, "_resolve_bundle_signer", lambda: _FakeBundleSigner())
    bundle = _write_bundle(tmp_path)

    workflow_handler(["sign", str(bundle), "--dir", str(tmp_path)])

    assert (bundle / "workflow.toml.arcsig").exists()


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


def test_workflow_handler_empty_args_prints_help_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        workflow_handler([])

    assert exc.value.code == 0
    assert "workflow" in capsys.readouterr().out.lower()
