"""H-025 — the semantic layer WIRED at the chokepoint, sample redaction, describe compose.

The semantic layer (``arcmemory.semantic_layer``) existed but had zero production
callers. This pins the three behaviors the wiring adds:

* ``register_datastore`` calls ``overlay()`` before ``persist_ontology`` — the
  editable file now actually gets created/updated on every real registration,
  keyed by the operator-facing ``connection_id`` (not the opaque source hash).
* Sampling is bounded, redacts a planted secret before it ever reaches the file,
  inherits the connection's classification, and is OFF by default at federal.
* ``describe_datastore`` composes the operator's meaning ahead of row data — it
  never replaces or shadows what a query actually returns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.semantic_layer import layer_for, layer_path

_DID = "did:arc:test-h025"


def _conn_with_secret() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, note TEXT)")
    # A real-shaped AWS access key — must never survive into the semantic layer
    # file unredacted (LLM02: this file is read by the agent on every DB search).
    conn.execute("INSERT INTO accounts VALUES ('a-1', 'key AKIA1234567890ABCDEF on file')")
    conn.execute("INSERT INTO accounts VALUES ('a-2', 'no secret here')")
    return conn


class TestChokepointWiring:
    async def test_register_datastore_writes_the_semantic_layer_file(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path / "ws", _DID)

        await brain.register_sqlite_datastore(
            "src-hash-abc123", _conn_with_secret(), connection_id="shop-db", caller_did=_DID
        )

        path = layer_path("shop-db")
        assert path is not None
        assert path.exists(), "overlay() must create the file, keyed by connection_id"
        assert "shop-db" in path.read_text(encoding="utf-8") or "accounts" in path.read_text(
            encoding="utf-8"
        )

    async def test_connection_id_keys_the_file_not_the_opaque_source_hash(
        self, tmp_path: Path
    ) -> None:
        brain = ArcMemoryBrain(tmp_path / "ws", _DID)

        await brain.register_sqlite_datastore(
            "a1b2c3-opaque-hash", _conn_with_secret(), connection_id="shop-db", caller_did=_DID
        )

        assert layer_path("a1b2c3-opaque-hash") != layer_path("shop-db")
        assert layer_path("shop-db") is not None
        assert layer_path("shop-db").exists()  # type: ignore[union-attr]
        assert not layer_path("a1b2c3-opaque-hash").exists()  # type: ignore[union-attr]


class TestSampling:
    async def test_a_planted_secret_is_redacted_before_it_reaches_the_file(
        self, tmp_path: Path
    ) -> None:
        brain = ArcMemoryBrain(
            tmp_path / "ws", _DID, config=MemoryConfig(datastore_sample_limit=5)
        )

        await brain.register_sqlite_datastore(
            "src-1", _conn_with_secret(), connection_id="shop-secret", caller_did=_DID
        )

        content = layer_path("shop-secret").read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "AKIA1234567890ABCDEF" not in content
        assert "[SECRET:AWS_ACCESS_KEY]" in content

    async def test_the_layer_inherits_the_connections_classification(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path / "ws", _DID)

        await brain.register_sqlite_datastore(
            "src-2", _conn_with_secret(), connection_id="shop-secret2", classification="secret"
        )

        layer = layer_for("shop-secret2")
        assert layer.classification == "secret"

    async def test_federal_tier_defaults_sampling_off(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path / "ws", _DID, config=MemoryConfig.for_tier("federal"))

        await brain.register_sqlite_datastore(
            "src-3", _conn_with_secret(), connection_id="shop-federal", caller_did=_DID
        )

        content = layer_path("shop-federal").read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "samples = [" not in content

    async def test_personal_tier_samples_by_default(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path / "ws", _DID)

        await brain.register_sqlite_datastore(
            "src-4", _conn_with_secret(), connection_id="shop-personal", caller_did=_DID
        )

        content = layer_path("shop-personal").read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "samples = [" in content


class TestDescribeComposes:
    async def test_describe_datastore_returns_operator_meaning(self, tmp_path: Path) -> None:
        brain = ArcMemoryBrain(tmp_path / "ws", _DID)
        await brain.register_sqlite_datastore(
            "src-5", _conn_with_secret(), connection_id="shop-describe", caller_did=_DID
        )

        text = await brain.describe_datastore("src-5", caller_did=_DID)

        assert "accounts" in text
        assert "one row is" in text

    async def test_describe_scoped_to_one_table_omits_others(self, tmp_path: Path) -> None:
        conn = _conn_with_secret()
        conn.execute("CREATE TABLE orders (id TEXT PRIMARY KEY)")
        brain = ArcMemoryBrain(tmp_path / "ws", _DID)
        await brain.register_sqlite_datastore(
            "src-6", conn, connection_id="shop-scoped", caller_did=_DID
        )

        text = await brain.describe_datastore("src-6", table="accounts", caller_did=_DID)

        assert "accounts" in text
        assert "orders" not in text

    async def test_query_result_composes_meaning_ahead_of_rows_never_replacing_them(
        self, tmp_path: Path
    ) -> None:
        """The compose point lives in arcagent's tool layer; here we prove the two
        primitives it composes — describe_datastore (meaning) and datastore_query
        (rows) — each still work standalone and neither's output subsumes the
        other, which is what "prepend, never shadow" requires of the seam."""
        brain = ArcMemoryBrain(tmp_path / "ws", _DID)
        await brain.register_sqlite_datastore(
            "src-7", _conn_with_secret(), connection_id="shop-compose", caller_did=_DID
        )

        meaning = await brain.describe_datastore("src-7", table="accounts", caller_did=_DID)
        row = await brain.datastore_query(
            "src-7", "get_record", "accounts", {"pk_value": "a-2"}, caller_did=_DID
        )

        # Compose, don't shadow: the meaning (schema-level, operator-authored) and
        # the row (exact typed lookup) are two independent, still-fully-working
        # primitives — the caller's tool layer prepends one to the other rather
        # than either one replacing what the other returns.
        assert "accounts" in meaning
        assert "one row is" in meaning
        assert row == {"id": "a-2", "note": "no secret here"}
