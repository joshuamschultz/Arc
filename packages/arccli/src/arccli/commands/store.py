"""``arc store`` — manual / air-gapped control over the operational store (FR-6).

arcstore is ambient: the agent lifecycle spins it up automatically. This group is
the explicit operator surface for the cases where that is not enough — an
air-gapped box, a compliance check, a forced re-ingest:

    arc store init       create the data-dir layout (spool / worm / store)
    arc store status     report paths, backend, and degraded state
    arc store verify     re-verify the WORM signed chain; non-zero exit on tamper
    arc store backfill   re-ingest spool + WORM into the queryable mirror
    arc store up         backfill then tail in the foreground (Ctrl+C to stop)

Every command resolves the data dir with the one shared rule
(``ARCSTORE_DATA_DIR`` env > ``--data-dir`` > ``[arcstore].data_dir`` > default)
so it always targets the same files a direct ``arc llm`` call wrote to.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from arcstore import ArcStoreConfig, resolve_data_dir


def _out(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _resolve_dir(args: argparse.Namespace) -> Path:
    """Shared data-dir resolution (env > --data-dir > default)."""
    return resolve_data_dir(getattr(args, "data_dir", None))


def _layout(data_dir: Path) -> dict[str, Path]:
    return {
        "spool": data_dir / "spool",
        "worm": data_dir / "worm",
        "store": data_dir / "store",
    }


def _ensure_layout(data_dir: Path) -> dict[str, Path]:
    """Create the spool / worm / store dirs idempotently."""
    layout = _layout(data_dir)
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    return layout


def _db_path(data_dir: Path) -> Path:
    """Per-instance SQLite file under the store dir (shared-nothing, NFR-8)."""
    return data_dir / "store" / "arcstore.db"


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def _init(args: argparse.Namespace) -> None:
    data_dir = _resolve_dir(args)
    layout = _ensure_layout(data_dir)
    _out(f"Initialized arcstore data dir: {data_dir}")
    for name, path in layout.items():
        _out(f"  {name}: {path}")


def _status(args: argparse.Namespace) -> None:
    data_dir = _resolve_dir(args)
    cfg = ArcStoreConfig()
    layout = _layout(data_dir)
    db = _db_path(data_dir)
    degraded = not db.exists()
    info = {
        "data_dir": str(data_dir),
        "backend": cfg.backend,
        "enabled": cfg.enabled,
        "spool": str(layout["spool"]),
        "worm": str(layout["worm"]),
        "store_db": str(db),
        "degraded": degraded,
    }
    if getattr(args, "json", False):
        _out(json.dumps(info, indent=2))
        return
    _out(f"arcstore status — data_dir: {data_dir}")
    _out(f"  backend:  {cfg.backend}")
    _out(f"  spool:    {layout['spool']}")
    _out(f"  worm:     {layout['worm']}")
    _out(f"  store db: {db}")
    _out(f"  degraded: {degraded}")


def _verify(args: argparse.Namespace) -> None:
    from arcstore.ingest import WORM_ACTIVE_FILENAME
    from arctrust.audit import verify_chain

    data_dir = _resolve_dir(args)
    worm = data_dir / "worm" / WORM_ACTIVE_FILENAME
    if not worm.exists():
        _err(f"No WORM chain found at {worm}")
        sys.exit(1)

    pubkey = _resolve_pubkey(args)
    ok = verify_chain(worm, pubkey)
    if ok:
        _out(f"WORM chain verified OK: {worm}")
        return
    _err(f"WORM chain verification FAILED (tamper detected): {worm}")
    sys.exit(2)


def _resolve_pubkey(args: argparse.Namespace) -> bytes:
    """Get the operator Ed25519 public key (--pubkey hex, or --did via trust store)."""
    pubkey_hex = getattr(args, "pubkey", None)
    if pubkey_hex:
        try:
            return bytes.fromhex(pubkey_hex)
        except ValueError:
            _err(f"--pubkey is not valid hex: {pubkey_hex!r}")
            sys.exit(1)
    did = getattr(args, "did", None)
    if did:
        from arctrust.trust_store import load_operator_pubkey

        try:
            return load_operator_pubkey(did)
        except Exception as exc:  # reason: surface a clear operator error, non-zero exit
            _err(f"Could not load operator public key for {did!r}: {exc}")
            sys.exit(1)
    _err("verify needs the operator public key: pass --pubkey <hex> or --did <operator-did>")
    sys.exit(1)


def _backfill(args: argparse.Namespace) -> None:
    data_dir = _resolve_dir(args)
    counts = asyncio.run(_run_backfill(data_dir, _resolve_pubkey_optional(args)))
    if getattr(args, "json", False):
        _out(json.dumps({"data_dir": str(data_dir), "counts": counts}, indent=2))
        return
    _out(f"Backfilled arcstore mirror from {data_dir}")
    for table, n in counts.items():
        _out(f"  {table}: {n}")


def _resolve_pubkey_optional(args: argparse.Namespace) -> bytes | None:
    """Pubkey for WORM verification during backfill — optional (None skips verify)."""
    if getattr(args, "pubkey", None) or getattr(args, "did", None):
        return _resolve_pubkey(args)
    return None


async def _run_backfill(data_dir: Path, worm_pubkey: bytes | None) -> dict[str, int]:
    from arcstore.backends import OPERATIONAL_TABLES, open_backend
    from arcstore.ingest import StoreIngest

    layout = _ensure_layout(data_dir)
    backend = open_backend("sqlite", _db_path(data_dir))
    await backend.start()
    try:
        ingest = StoreIngest(
            backend,
            spool_dir=layout["spool"],
            worm_dir=layout["worm"],
            worm_public_key=worm_pubkey,
        )
        await ingest.backfill()
        counts: dict[str, int] = {}
        for table in (*OPERATIONAL_TABLES, "audit_chain"):
            rows = await backend.query(table)
            counts[table] = len(rows)
        return counts
    finally:
        await backend.stop()


def _up(args: argparse.Namespace) -> None:
    data_dir = _resolve_dir(args)
    _out(f"Starting arcstore ingest (backfill + tail) on {data_dir}. Ctrl+C to stop.")
    try:
        asyncio.run(_run_up(data_dir, _resolve_pubkey_optional(args)))
    except KeyboardInterrupt:
        _out("\nStopped.")


async def _run_up(data_dir: Path, worm_pubkey: bytes | None) -> None:
    from arcstore.backends import open_backend
    from arcstore.ingest import StoreIngest

    layout = _ensure_layout(data_dir)
    backend = open_backend("sqlite", _db_path(data_dir))
    await backend.start()
    ingest = StoreIngest(
        backend,
        spool_dir=layout["spool"],
        worm_dir=layout["worm"],
        worm_public_key=worm_pubkey,
    )
    await ingest.start()
    try:
        await asyncio.Event().wait()
    finally:
        await ingest.stop()
        await backend.stop()


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_SUBCOMMANDS = {
    "init": _init,
    "status": _status,
    "verify": _verify,
    "backfill": _backfill,
    "up": _up,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc store",
        description="Operational store lifecycle — init, status, verify, backfill, up.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--data-dir",
            dest="data_dir",
            default=None,
            help="Arc data dir (default: env ARCSTORE_DATA_DIR or ~/.arc/store).",
        )

    init_p = subs.add_parser("init", help="Create the data-dir layout.")
    _common(init_p)

    status_p = subs.add_parser("status", help="Report store paths and state.")
    _common(status_p)
    status_p.add_argument("--json", action="store_true", help="Emit JSON.")

    verify_p = subs.add_parser("verify", help="Verify the WORM signed chain.")
    _common(verify_p)
    verify_p.add_argument("--pubkey", default=None, help="Operator Ed25519 public key (hex).")
    verify_p.add_argument("--did", default=None, help="Operator DID (resolved via trust store).")

    backfill_p = subs.add_parser("backfill", help="Re-ingest spool + WORM into the mirror.")
    _common(backfill_p)
    backfill_p.add_argument("--json", action="store_true", help="Emit JSON.")
    backfill_p.add_argument("--pubkey", default=None, help="Operator pubkey (hex) for WORM check.")
    backfill_p.add_argument("--did", default=None, help="Operator DID for WORM verify.")

    up_p = subs.add_parser("up", help="Backfill then tail in the foreground.")
    _common(up_p)
    up_p.add_argument("--pubkey", default=None, help="Operator pubkey (hex) for WORM verify.")
    up_p.add_argument("--did", default=None, help="Operator DID for WORM verify.")

    return parser


def store_handler(args: list[str]) -> None:
    """Entry point for ``arc store <subcommand>`` (registry dispatch)."""
    parser = _build_parser()
    if not args:
        parser.print_help()
        return
    ns = parser.parse_args(args)
    if ns.subcmd is None:
        parser.print_help()
        return
    handler = _SUBCOMMANDS.get(ns.subcmd)
    if handler is None:
        _err(f"arc store: unknown subcommand {ns.subcmd!r}")
        sys.exit(1)
    handler(ns)
