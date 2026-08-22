from pathlib import Path


def test_knowledge_has_no_upward_memory_or_team_imports() -> None:
    root = Path(__file__).parents[2] / "src" / "arcagent" / "knowledge"
    for source in root.rglob("*.py"):
        text = source.read_text()
        assert "arcteam" not in text
        assert "arcmemory" not in text
