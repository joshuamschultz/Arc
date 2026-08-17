"""Published digests — pointers cross the privacy boundary, contents never do."""

from __future__ import annotations

import pytest

from arcteam.digest import (
    MAX_ENTRIES,
    AgentDigest,
    DigestEntry,
    DigestStore,
    extract_entities,
    summarize_artifact,
)
from arcteam.storage import MemoryBackend


@pytest.fixture
def store() -> DigestStore:
    return DigestStore(MemoryBackend())


class TestSummarizingAnArtifact:
    def test_it_publishes_the_title_and_never_the_body(self) -> None:
        entry = summarize_artifact(
            "NNL technical requirements\n\nSECRET: the reactor runs at 400 degrees.",
            artifact_id="doc-1",
        )

        assert entry.title == "NNL technical requirements"
        assert "400 degrees" not in entry.model_dump_json()
        assert "reactor" not in entry.title

    def test_it_keeps_the_rare_identifier(self) -> None:
        """The whole point: NNL must survive into what other agents can rank."""
        entry = summarize_artifact("NNL technical requirements for the site", artifact_id="d")

        assert "NNL" in entry.entities

    def test_it_costs_no_model_call(self) -> None:
        """Written at ingest, so it must be pure — this runs on every capture."""
        assert summarize_artifact("anything at all", artifact_id="d").title == "anything at all"

    def test_a_long_title_is_bounded(self) -> None:
        entry = summarize_artifact("x" * 900, artifact_id="d")

        assert len(entry.title) == 200


class TestExtractEntities:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("NNL requirements", "NNL"),
            ("the ArcTeam roster", "ArcTeam"),
            ("Quarterly review", "Quarterly"),
        ],
    )
    def test_it_finds_the_nameable_tokens(self, text: str, expected: str) -> None:
        assert expected in extract_entities(text)

    def test_it_does_not_grow_without_bound(self) -> None:
        assert len(extract_entities(" ".join(f"Name{i}x" for i in range(200)))) <= 24


class TestDigestDocument:
    def test_it_ranks_over_titles_entities_and_tags(self) -> None:
        digest = AgentDigest(
            agent_did="did:arc:ops",
            entries=[DigestEntry(artifact_id="a", title="reqs", entities=["NNL"], tags=["site"])],
        )

        assert digest.as_document() == "reqs NNL site"


class TestStore:
    async def test_a_published_digest_reads_back(self, store: DigestStore) -> None:
        await store.publish(AgentDigest(agent_did="did:arc:ops", handle="ops"))

        assert (await store.get("did:arc:ops")) is not None

    async def test_an_unpublished_agent_has_none(self, store: DigestStore) -> None:
        assert (await store.get("did:arc:nobody")) is None

    async def test_every_digest_is_the_candidate_set(self, store: DigestStore) -> None:
        await store.publish(AgentDigest(agent_did="did:arc:a", handle="a"))
        await store.publish(AgentDigest(agent_did="did:arc:b", handle="b"))

        assert {d.handle for d in await store.list_digests()} == {"a", "b"}

    async def test_refiling_updates_a_pointer_rather_than_adding_one(
        self, store: DigestStore
    ) -> None:
        """A digest listing the same document twice ranks its owner up for nothing."""
        await store.add_entry("did:arc:ops", "ops", DigestEntry(artifact_id="d", title="v1"))
        digest = await store.add_entry(
            "did:arc:ops", "ops", DigestEntry(artifact_id="d", title="v2")
        )

        assert [entry.title for entry in digest.entries] == ["v2"]

    async def test_the_newest_pointer_comes_first(self, store: DigestStore) -> None:
        await store.add_entry("did:arc:ops", "ops", DigestEntry(artifact_id="a", title="old"))
        digest = await store.add_entry(
            "did:arc:ops", "ops", DigestEntry(artifact_id="b", title="new")
        )

        assert [entry.title for entry in digest.entries] == ["new", "old"]

    async def test_a_digest_is_an_index_not_an_archive(self, store: DigestStore) -> None:
        digest = AgentDigest(agent_did="did:arc:ops", handle="ops")
        for i in range(MAX_ENTRIES + 40):
            digest = digest.with_entry(DigestEntry(artifact_id=f"a{i}", title=f"t{i}"))

        assert len(digest.entries) == MAX_ENTRIES

    async def test_an_agent_can_only_publish_under_its_own_did(self, store: DigestStore) -> None:
        """The key comes from the digest, so publishing for someone else is inexpressible."""
        await store.publish(AgentDigest(agent_did="did:arc:ops", handle="pretending-to-be-hr"))

        assert (await store.get("did:arc:hr")) is None
        assert (await store.get("did:arc:ops")) is not None
