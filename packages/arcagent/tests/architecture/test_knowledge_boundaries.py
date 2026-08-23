from pathlib import Path


def test_knowledge_contracts_have_no_upward_memory_or_team_imports() -> None:
    root = Path(__file__).parents[2] / "src" / "arcagent" / "knowledge"
    for source in root.rglob("*.py"):
        text = source.read_text()
        assert "arcteam" not in text
        assert "arcmemory" not in text


def test_memory_module_has_no_fleet_shared_knowledge_construction() -> None:
    root = Path(__file__).parents[2] / "src" / "arcagent" / "modules" / "memory"
    for source in root.rglob("*.py"):
        text = source.read_text()
        assert "arcteam" not in text
        assert "FleetSharedKnowledgeBackend" not in text
        assert "shared_knowledge_enabled" not in text
