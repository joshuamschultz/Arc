"""Tests for the ``arc store`` command group (SPEC-026 FR-6).

``arc store`` is the manual/air-gapped control surface over the ambient
operational store: init the data dir, report status, verify the WORM chain,
and backfill the queryable mirror from the durable files. Each command takes
``--data-dir`` so the shared default (~/.arc/store) is never touched in tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arccli.commands.store import store_handler


def _worm_file(data_dir: Path) -> Path:
    return data_dir / "worm" / "audit-chain.jsonl"


def _seed_valid_worm(data_dir: Path) -> bytes:
    """Write a real signed WORM chain via arctrust; return the public key."""
    from arctrust.audit import AuditEvent, WormSink
    from arctrust.keypair import generate_keypair

    kp = generate_keypair()
    worm_dir = data_dir / "worm"
    worm_dir.mkdir(parents=True, exist_ok=True)
    sink = WormSink(_worm_file(data_dir), kp.private_key)
    sink.write(AuditEvent(actor_did="did:arc:1", action="tool_call", target="fs", outcome="allow"))
    sink.write(
        AuditEvent(actor_did="did:arc:1", action="tool_call", target="net", outcome="allow")
    )
    sink.close()
    return kp.public_key


# -- init ---------------------------------------------------------------------


def test_init_creates_layout(tmp_path: Path) -> None:
    store_handler(["init", "--data-dir", str(tmp_path)])
    assert (tmp_path / "spool").is_dir()
    assert (tmp_path / "worm").is_dir()
    assert (tmp_path / "store").is_dir()


def test_init_is_idempotent(tmp_path: Path) -> None:
    store_handler(["init", "--data-dir", str(tmp_path)])
    store_handler(["init", "--data-dir", str(tmp_path)])  # no raise on second run
    assert (tmp_path / "spool").is_dir()


# -- status -------------------------------------------------------------------


def test_status_reports_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store_handler(["status", "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert str(tmp_path) in out
    assert "spool" in out
    assert "sqlite" in out.lower()


# -- verify -------------------------------------------------------------------


def test_verify_passes_on_intact_chain(tmp_path: Path) -> None:
    pub = _seed_valid_worm(tmp_path)
    # exit 0 — no SystemExit raised
    store_handler(["verify", "--data-dir", str(tmp_path), "--pubkey", pub.hex()])


def test_verify_exit_code_nonzero_on_tamper(tmp_path: Path) -> None:
    pub = _seed_valid_worm(tmp_path)
    worm = _worm_file(tmp_path)
    raw = worm.read_bytes()
    # Flip a byte in the first record's payload — breaks the hash chain.
    tampered = bytearray(raw)
    idx = raw.index(b"tool_call")
    tampered[idx] = ord("X")
    worm.write_bytes(bytes(tampered))
    with pytest.raises(SystemExit) as exc:
        store_handler(["verify", "--data-dir", str(tmp_path), "--pubkey", pub.hex()])
    assert exc.value.code != 0


def test_verify_exit_nonzero_when_worm_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        store_handler(["verify", "--data-dir", str(tmp_path), "--pubkey", "00" * 32])
    assert exc.value.code != 0


# -- backfill -----------------------------------------------------------------


def _seed_spool(data_dir: Path) -> None:
    """Write one llm_call spool record directly (no running store)."""
    from arcstore.records import SpoolRecord
    from arcstore.spool import record, spool_path

    record(
        SpoolRecord(
            kind="llm_call",
            actor_did="did:arc:1",
            model="gpt",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.01,
        ),
        path=spool_path(data_dir=data_dir),
    )


def test_backfill_ingests_spool_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_spool(tmp_path)
    store_handler(["backfill", "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    # The spool llm_call must land in the queryable mirror.
    assert "llm_calls" in out
    assert (tmp_path / "store").is_dir()


def test_backfill_then_query_roundtrip(tmp_path: Path) -> None:
    """backfill is idempotent and the row is queryable afterwards (UC-1)."""
    import asyncio

    from arcstore.backends import open_backend

    _seed_spool(tmp_path)
    store_handler(["backfill", "--data-dir", str(tmp_path)])
    store_handler(["backfill", "--data-dir", str(tmp_path)])  # idempotent

    db = next((tmp_path / "store").glob("*.db"))

    async def _count() -> int:
        backend = open_backend("sqlite", db)
        await backend.start()
        try:
            rows = await backend.query("llm_calls")
            return len(rows)
        finally:
            await backend.stop()

    assert asyncio.run(_count()) == 1  # no duplicate from the second backfill


# -- dispatch -----------------------------------------------------------------


def test_unknown_subcommand_exits_nonzero(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        store_handler(["frobnicate"])
    assert exc.value.code != 0


def test_no_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    store_handler([])
    out = capsys.readouterr().out
    assert "store" in out.lower()


def test_store_registered_in_command_registry() -> None:
    """The ``store`` group is reachable from the central registry (REPL + CLI)."""
    from arccli.commands.registry import resolve_command

    cmd = resolve_command("store")
    assert cmd is not None
    assert cmd.handler is not None


def test_backfill_json_payload_is_machine_readable(tmp_path: Path) -> None:
    """``--json`` emits a parseable object for scripting/air-gapped tooling."""
    _seed_spool(tmp_path)
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        store_handler(["backfill", "--data-dir", str(tmp_path), "--json"])
    payload = json.loads(buf.getvalue())
    assert payload["counts"]["llm_calls"] == 1
