"""G12 (J1): document_search ranks the right page high with the production embedder.

Seeds 50 wiki pages through the real approved-ingest path, then asks 10
paraphrased questions (they share few words with the page) with no source named.
The correct page must be in the top 3 for at least 8 of 10, with no duplicate
pointers. Uses the real local all-MiniLM-L6-v2 model; when its weights are not
in the offline cache the test skips, it never fakes the embedder.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcmemory.config import MemoryConfig
from arcmemory.connected_data import (
    ConnectedDataService,
    ConnectedObject,
    ConnectedSource,
    ConnectedSourceShape,
    SourceContent,
    SourceMappingPendingError,
)
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex

_DID = "did:arc:relevance-agent"

_PAGES: dict[str, str] = {
    "vpn-setup": "How to install the corporate VPN client and sign in with your badge token.",
    "expense-policy": "Employees are reimbursed for travel meals up to seventy five dollars a day.",
    "onboarding-week-one": "New hires receive a laptop, accounts, and a buddy during their first week.",
    "incident-response": "When production goes down the on-call engineer opens a war room and pages the lead.",
    "parental-leave": "Birth and adoption leave grants sixteen weeks of paid time away from work.",
    "password-rotation": "Credentials must be changed every ninety days and never reused.",
    "office-wifi": "Guests connect to the visitor network using the daily code at the front desk.",
    "code-review-guide": "Every pull request needs two approvals and a passing build before merge.",
    "release-checklist": "Before shipping, tag the version, update the changelog, and run smoke tests.",
    "security-training": "All staff complete annual phishing awareness courses by the end of March.",
    "data-retention": "Customer records are deleted seven years after the account is closed.",
    "oncall-rotation": "Engineers take the pager one week in six and hand off on Monday morning.",
    "vendor-approval": "New suppliers require a security questionnaire and a signed data agreement.",
    "brand-colors": "The logo uses deep navy with a bright teal accent on white backgrounds.",
    "press-inquiries": "Reporters asking for comment should be sent to the communications director.",
    "holiday-calendar": "The company observes eleven public holidays plus a winter shutdown.",
    "remote-work": "Staff may work from home three days a week with manager agreement.",
    "bug-triage": "Severity one defects are fixed within a day; minor ones wait for the next sprint.",
    "api-rate-limits": "Clients are limited to one thousand requests per minute per access key.",
    "database-backups": "Nightly snapshots are copied to cold storage and kept for thirty five days.",
    "disaster-recovery": "The failover site must resume service within four hours of a regional outage.",
    "sales-commission": "Account executives earn ten percent of first year contract value as commission.",
    "customer-refunds": "Refund requests within thirty days of purchase are approved without questions.",
    "support-escalation": "Tickets unanswered for two hours move up to the senior support tier.",
    "pricing-tiers": "The starter plan costs nineteen dollars, the team plan ninety nine monthly.",
    "competitor-notes": "Our main rival undercuts on price but lacks audit logging features.",
    "hiring-process": "Candidates complete a phone screen, a take home task, and four onsite interviews.",
    "performance-reviews": "Managers write twice yearly evaluations and calibrate ratings with peers.",
    "equity-grants": "Stock options vest over four years with a one year cliff.",
    "health-insurance": "Medical, dental and vision coverage begins on the first day of employment.",
    "learning-budget": "Each person has two thousand dollars annually for books and conferences.",
    "mobile-app-release": "iOS builds go through TestFlight beta for a week before store submission.",
    "design-system": "Buttons, inputs, and spacing tokens live in one shared component library.",
    "accessibility-standards": "Every screen must be usable with a keyboard and meet contrast guidelines.",
    "localization": "Strings are extracted for translators and shipped in twelve languages.",
    "analytics-events": "Product usage is tracked with named events sent to the warehouse hourly.",
    "feature-flags": "New functionality is hidden behind toggles and rolled out to ten percent first.",
    "load-testing": "Before launch, simulate fifty thousand concurrent users against staging.",
    "monitoring-alerts": "Pager alerts fire when error rate exceeds two percent for five minutes.",
    "log-retention": "Application logs are searchable for ninety days and then archived.",
    "cloud-costs": "Finance reviews the monthly cloud bill and flags any team over budget.",
    "contract-templates": "Legal maintains standard agreements for customers, partners and contractors.",
    "trademark-usage": "Partners may display our mark only with written permission and approved artwork.",
    "meeting-norms": "Meetings need an agenda and end with written decisions and owners.",
    "travel-booking": "Flights and hotels are reserved through the approved agency portal.",
    "equipment-return": "Departing staff must ship their laptop and badge back within five days.",
    "bug-bounty": "Researchers who report valid vulnerabilities receive rewards up to ten thousand dollars.",
    "open-source-policy": "Engineers may contribute to public projects after a licensing review.",
    "quarterly-planning": "Teams submit goals in the first week of each quarter for leadership review.",
    "customer-interviews": "Product managers speak with five customers every month about their problems.",
    "office-safety": "Fire exits are marked, and evacuation drills happen every six months.",
}

_QUERIES: list[tuple[str, str]] = [
    ("how do I connect to the company network from home", "vpn-setup"),
    ("what am I allowed to claim for dinner on a business trip", "expense-policy"),
    ("how long can new parents stay out", "parental-leave"),
    ("how many people must sign off before code goes in", "code-review-guide"),
    ("how often do I need a new login secret", "password-rotation"),
    ("what happens when the website crashes at night", "incident-response"),
    ("how long do we keep user data after they leave", "data-retention"),
    ("can I get my money back after buying", "customer-refunds"),
    ("how quickly must we be back up if a data center fails", "disaster-recovery"),
    ("how many days a week can I work outside the office", "remote-work"),
]


@pytest.fixture(scope="module")
def real_embedder() -> object:
    """The production local embedder, or a skip when its weights are not cached."""
    arcllm_embeddings = pytest.importorskip("arcllm.embeddings")
    pytest.importorskip("sentence_transformers")
    local = arcllm_embeddings.LocalEmbedder(arcllm_embeddings.DEFAULT_EMBED_MODEL)

    class _Embedder:
        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            response = await local.embed(texts)
            return [list(vector) for vector in response.vectors]

    embedder = _Embedder()
    try:
        import asyncio

        asyncio.run(embedder.embed_texts(["probe"]))
    except arcllm_embeddings.ArcLLMEmbeddingUnavailableError as error:
        pytest.skip(f"all-MiniLM-L6-v2 weights are not in the offline model cache: {error}")
    return embedder


async def test_g12_the_right_page_is_in_the_top_three_for_eight_of_ten(
    tmp_path: Path, real_embedder: object
) -> None:
    source = ConnectedSource(
        connection_id="confluence",
        account_id="acct",
        source_kind="confluence",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(
        tmp_path,
        _DID,
        approval_store=approval,
        config=MemoryConfig(),
        embedder=real_embedder,  # type: ignore[arg-type]  # structural Embedder
    )
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)
    for page_id, body in _PAGES.items():
        await service.ingest(
            source,
            ConnectedObject(
                object_id=page_id,
                locator=f"/pages/{page_id}",
                version="1",
                media_type="text/plain",
                classification="unclassified",
                metadata={"title": page_id},
            ),
            SourceContent(
                object_id=page_id, version="1", media_type="text/plain", content=body.encode()
            ),
            mapping,
        )
    service.close()

    index = DocIndex(
        MemoryDB(tmp_path),
        tmp_path,
        MemoryConfig(),
        embedder=real_embedder,  # type: ignore[arg-type]  # structural Embedder
    )
    found = 0
    for question, page_id in _QUERIES:
        hits = await index.document_search(question, _DID, top_k=3)
        pointers = [hit.pointer for hit in hits]
        assert len(pointers) == len(set(pointers)), f"duplicate pointers for {question!r}"
        found += any(hit.title == page_id for hit in hits)

    assert found >= 8, f"only {found}/10 queries put the right page in the top 3"
