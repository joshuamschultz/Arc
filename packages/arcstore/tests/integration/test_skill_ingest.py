"""arcstore ingestion of the arcskill candidate store (SPEC-054 REQ-120 / COMP-010).

The on-disk layout mirrored by these fixtures is owned by
``arcskill.improver.candidate_store.CandidateStore``:
``<workspace>/skill_traces/<skill>/candidates/{<id>.md, manifest.json}`` plus the
operator-signed skills WORM chain at ``<workspace>/../.audit/skills.worm``
(written by ``arcagent.modules.skills._runtime``). arcstore never imports
arcskill — the tailer reads the durable files only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from arcstore import query
from arcstore.backends.sqlite import SqliteBackend
from arcstore.ingest import StoreIngest

SKILL = "myskill"


def _candidates_dir(workspace: Path, skill: str = SKILL) -> Path:
    d = workspace / "skill_traces" / skill / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_manifest(
    workspace: Path, manifest: dict[str, Any], *, mtime_ns: int, skill: str = SKILL
) -> None:
    """Write the manifest with an explicit mtime so change-detection is deterministic."""
    path = _candidates_dir(workspace, skill) / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _manifest(candidates: dict[str, Any], active: str | None = None) -> dict[str, Any]:
    return {
        "skill_name": SKILL,
        "frontier": [],
        "candidates": candidates,
        "active_candidate_id": active,
        "lifecycle_state": "active",
    }


async def _make_ingest(tmp_path: Path) -> tuple[StoreIngest, SqliteBackend, Path]:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "agent" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(data_dir / "store" / "inst.db")
    await backend.start()
    ingest = StoreIngest(
        backend,
        spool_dir=data_dir / "spool",
        worm_dir=data_dir / "worm",
        workspace_dir=workspace,
    )
    return ingest, backend, workspace


class TestCandidateIngest:
    async def test_manifest_and_bodies_ingested(self, tmp_path: Path) -> None:
        """Candidates + bodies land in the store; versions list is metadata by generation."""
        ingest, backend, workspace = await _make_ingest(tmp_path)
        try:
            cdir = _candidates_dir(workspace)
            (cdir / "aaa111.md").write_text("seed text v1", encoding="utf-8")
            (cdir / "bbb222.md").write_text("improved text v2", encoding="utf-8")
            _write_manifest(
                workspace,
                _manifest(
                    {
                        "bbb222": {
                            "generation": 2,
                            "parent_id": "aaa111",
                            "scores": {"pass_rate": 0.9},
                        },
                        "aaa111": {"generation": 1, "parent_id": None, "scores": {}},
                    },
                    active="bbb222",
                ),
                mtime_ns=1_000,
            )
            await ingest.backfill()

            versions = await query.skill_versions(backend, SKILL)
            assert [v["candidate_id"] for v in versions] == ["aaa111", "bbb222"]
            first, second = versions
            assert first["generation"] == 1
            assert first["parent_id"] is None
            assert first["active"] is False
            assert second["generation"] == 2
            assert second["parent_id"] == "aaa111"
            assert second["scores"] == {"pass_rate": 0.9}
            assert second["active"] is True
            expected_hash = hashlib.sha256(b"improved text v2").hexdigest()
            assert second["body_hash"] == expected_hash
            assert "body" not in second  # metadata-only list payload

            body = await query.skill_candidate_body(backend, SKILL, "bbb222")
            assert body == "improved text v2"
        finally:
            await backend.stop()

    async def test_reingest_is_idempotent(self, tmp_path: Path) -> None:
        """Replaying the same manifest (even with a new mtime) never duplicates rows."""
        ingest, backend, workspace = await _make_ingest(tmp_path)
        try:
            cdir = _candidates_dir(workspace)
            (cdir / "aaa111.md").write_text("text", encoding="utf-8")
            manifest = _manifest(
                {"aaa111": {"generation": 1, "parent_id": None, "scores": {}}}, active="aaa111"
            )
            _write_manifest(workspace, manifest, mtime_ns=1_000)
            await ingest.backfill()
            await ingest.backfill()
            # Identical content, changed mtime — re-read, but content-keyed rows dedupe.
            _write_manifest(workspace, manifest, mtime_ns=2_000)
            await ingest.backfill()
            rows = await backend.query("skill_candidates")
            assert len(rows) == 1
        finally:
            await backend.stop()

    async def test_missing_body_is_pending(self, tmp_path: Path) -> None:
        """A manifest id with no candidates/<id>.md still yields a queryable metadata row."""
        ingest, backend, workspace = await _make_ingest(tmp_path)
        try:
            _write_manifest(
                workspace,
                _manifest({"ccc333": {"generation": 3, "parent_id": None, "scores": {}}}),
                mtime_ns=1_000,
            )
            await ingest.backfill()
            versions = await query.skill_versions(backend, SKILL)
            assert len(versions) == 1
            assert versions[0]["candidate_id"] == "ccc333"
            assert versions[0]["body_hash"] is None
            assert await query.skill_candidate_body(backend, SKILL, "ccc333") is None
        finally:
            await backend.stop()

    async def test_manifest_reread_only_when_changed(self, tmp_path: Path) -> None:
        """An unchanged manifest mtime skips the skill entirely — no file re-reads."""
        ingest, backend, workspace = await _make_ingest(tmp_path)
        try:
            cdir = _candidates_dir(workspace)
            (cdir / "aaa111.md").write_text("original", encoding="utf-8")
            _write_manifest(
                workspace,
                _manifest({"aaa111": {"generation": 1, "parent_id": None, "scores": {}}}),
                mtime_ns=1_000,
            )
            await ingest.backfill()
            # Mutate the body WITHOUT touching the manifest: the skill is skipped,
            # so the stored body stays at the first-read content.
            (cdir / "aaa111.md").write_text("mutated behind the manifest", encoding="utf-8")
            await ingest.backfill()
            assert await query.skill_candidate_body(backend, SKILL, "aaa111") == "original"
        finally:
            await backend.stop()

    async def test_incremental_new_candidate_and_active_flip(self, tmp_path: Path) -> None:
        """A manifest change ingests the new candidate; a rollback flips the active flag."""
        ingest, backend, workspace = await _make_ingest(tmp_path)
        try:
            cdir = _candidates_dir(workspace)
            (cdir / "aaa111.md").write_text("v1", encoding="utf-8")
            _write_manifest(
                workspace,
                _manifest(
                    {"aaa111": {"generation": 1, "parent_id": None, "scores": {}}},
                    active="aaa111",
                ),
                mtime_ns=1_000,
            )
            await ingest.backfill()

            (cdir / "bbb222.md").write_text("v2", encoding="utf-8")
            _write_manifest(
                workspace,
                _manifest(
                    {
                        "aaa111": {"generation": 1, "parent_id": None, "scores": {}},
                        "bbb222": {"generation": 2, "parent_id": "aaa111", "scores": {}},
                    },
                    active="bbb222",
                ),
                mtime_ns=2_000,
            )
            await ingest.backfill()
            versions = await query.skill_versions(backend, SKILL)
            active = {v["candidate_id"]: v["active"] for v in versions}
            assert active == {"aaa111": False, "bbb222": True}

            # Rollback (non-destructive manifest flip) — latest state wins on read.
            _write_manifest(
                workspace,
                _manifest(
                    {
                        "aaa111": {"generation": 1, "parent_id": None, "scores": {}},
                        "bbb222": {"generation": 2, "parent_id": "aaa111", "scores": {}},
                    },
                    active="aaa111",
                ),
                mtime_ns=3_000,
            )
            await ingest.backfill()
            versions = await query.skill_versions(backend, SKILL)
            active = {v["candidate_id"]: v["active"] for v in versions}
            assert active == {"aaa111": True, "bbb222": False}
        finally:
            await backend.stop()

    async def test_unsafe_candidate_id_is_skipped(self, tmp_path: Path) -> None:
        """A poisoned manifest id must never drive a file read outside candidates/ (ASI06)."""
        ingest, backend, workspace = await _make_ingest(tmp_path)
        try:
            secret = tmp_path / "secret.md"
            secret.write_text("must never be ingested", encoding="utf-8")
            _write_manifest(
                workspace,
                _manifest(
                    {
                        "../../../secret": {"generation": 1, "parent_id": None, "scores": {}},
                        "aaa111": {"generation": 1, "parent_id": None, "scores": {}},
                    }
                ),
                mtime_ns=1_000,
            )
            await ingest.backfill()
            versions = await query.skill_versions(backend, SKILL)
            assert [v["candidate_id"] for v in versions] == ["aaa111"]
            bodies = await backend.query("skill_candidate_bodies")
            assert all("never be ingested" not in (r.get("body") or "") for r in bodies)
        finally:
            await backend.stop()

    async def test_no_workspace_configured_is_noop(self, tmp_path: Path) -> None:
        """Without workspace_dir the candidate scan is off — existing callers unaffected."""
        data_dir = tmp_path / "data"
        backend = SqliteBackend(data_dir / "store" / "inst.db")
        await backend.start()
        ingest = StoreIngest(backend, spool_dir=data_dir / "spool", worm_dir=data_dir / "worm")
        try:
            await ingest.backfill()
            assert await backend.query("skill_candidates") == []
        finally:
            await backend.stop()


class TestSkillsWormIngest:
    async def test_skills_worm_chain_ingested_and_verified(self, tmp_path: Path) -> None:
        """The skills WORM chain at <workspace>/../.audit/skills.worm rides the same
        audit ingest path: byte-cursor tailed, chain-verified, mirrored to audit_chain."""
        from arctrust.audit import AuditEvent, WormSink
        from arctrust.keypair import generate_keypair
        from arctrust.signer import InProcessSigner

        data_dir = tmp_path / "data"
        workspace = tmp_path / "agent" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        audit_dir = workspace.parent / ".audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        kp = generate_keypair()
        sink = WormSink(audit_dir / "skills.worm", InProcessSigner(kp.private_key))
        for i in range(2):
            sink.write(
                AuditEvent(
                    actor_did=f"did:arc:test:exec/{i:08x}",
                    action="skill.mutation.applied",
                    target=SKILL,
                    outcome="adopted",
                )
            )
        sink.close()

        backend = SqliteBackend(data_dir / "store" / "inst.db")
        await backend.start()
        ingest = StoreIngest(
            backend,
            spool_dir=data_dir / "spool",
            worm_dir=data_dir / "worm",
            worm_public_key=kp.public_key,
            workspace_dir=workspace,
        )
        try:
            await ingest.backfill()
            await ingest.backfill()  # replay must not duplicate
            rows = await backend.query("audit_chain")
            assert len(rows) == 2
            assert all(r["verified"] for r in rows)
            assert all(r["action"] == "skill.mutation.applied" for r in rows)
        finally:
            await backend.stop()
