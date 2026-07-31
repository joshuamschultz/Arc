# Developer Contribution Guidelines

> **Section:** 3. Reference · **Topic:** Development
> **Who this is for:** Contributors to the Arc codebase.
> **Read this after:** [DEPLOYMENT.md](DEPLOYMENT.md) · **Read this next:** [TESTING.md](TESTING.md)
> **See also:** [13-contributing.md](13-contributing.md) for the core contribution doc

---

## Development Setup

### Prerequisites

```bash
# Required
python >= 3.11
uv >= 0.4.0
docker >= 24.0

# Optional (for full testing)
ollama >= 0.1.0  # For local models
nats-server >= 2.10  # For team testing
```

### Install Development Environment

```bash
# Clone repository
git clone https://github.com/joshuamschultz/Arc.git
cd Arc

# Install all packages in editable mode
uv sync --all-packages

# Install pre-commit hooks
pre-commit install
```

---

## Project Structure

```
Arc/
├── packages/
│   ├── arctrust/          # Security primitives
│   ├── arcstore/          # Storage backend
│   ├── arcllm/            # LLM client
│   ├── arcmodel/          # Model registry
│   ├── arcprompt/         # Prompt engine
│   ├── arcrun/            # Execution loop
│   ├── arcskill/          # Skill hub
│   ├── arcmemory/         # Memory system
│   ├── arcteam/           # Multi-agent
│   ├── arcagent/          # Agent framework
│   ├── arccli/            # CLI
│   ├── arctui/            # Terminal UI
│   ├── arcui/             # Dashboard
│   ├── arcgateway/        # Gateway daemon
│   ├── arcgateway-telegram/
│   ├── arcgateway-slack/
│   ├── arcgateway-mattermost/
│   └── arcmas/            # Metapackage
├── tests/
│   ├── architecture/      # Layering tests
│   └── integration/         # Integration tests
├── docs/
└── walkthroughs/            # Jupyter notebooks
```

---

## Architecture Rules

### Layering Constraints

```mermaid
flowchart TB
    classDef valid fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef invalid fill:#F68D2E,stroke:#C06000,color:#FFFFFF

    arctrust[arctrust]:::valid --> arcagent[arcagent]:::valid
    arcagent --> arctrust
    
    arcgateway[arcgateway]:::invalid -.-> arcagent[arcagent]:::invalid
    "❌ No arcagent imports":::invalid
```

### Valid Import Patterns

| Package | Can Import |
|---------|------------|
| `arctrust` | None |
| `arcstore` | `arctrust` |
| `arcllm` | `arctrust`, `arcstore` |
| `arcprompt` | None |
| `arcrun` | `arcllm`, `arctrust`, `arcstore` |
| `arcskill` | `arctrust` |
| `arcmemory` | `arcstore` |
| `arcteam` | `arctrust`, `arcstore` |
| `arcagent` | `arcrun`, `arcllm`, `arctrust`, `arcstore`, `arcskill`, `arcteam`, `arcmemory` |
| `arccli` | `arcagent`, `arcteam`, `arcskill` |
| `arctui` | `arcagent` |
| `arcui` | `arcagent` |
| `arcgateway` | `arcagent` |

---

## Code Standards

### Type Checking

```bash
# Run mypy strict
uv run mypy --strict packages/arctrust

# All packages must pass
uv run mypy --strict packages/*/
```

### Linting

```bash
# Run ruff
uv run ruff check packages/

# Fix issues
uv run ruff check --fix packages/
```

### Formatting

```bash
# Format with ruff
uv run ruff format packages/
```

### Pre-commit Hooks

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.0
    hooks:
      - id: ruff
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.0
    hooks:
      - id: mypy
```

---

## Testing

### Run Tests

```bash
# All tests
uv run pytest tests/

# Specific package
uv run pytest packages/arctrust/tests/

# With coverage
uv run pytest --cov=packages/arctrust --cov-report=html

# Watch mode
uv run pytest --watch
```

### Test Categories

```bash
# Unit tests
uv run pytest tests/unit/

# Integration tests
uv run pytest tests/integration/

# Architecture tests
uv run pytest tests/architecture/
```

### Writing Tests

```python
# tests/test_my_feature.py
import pytest
from arcagent import ArcAgent

@pytest.mark.asyncio
async def test_agent_startup():
    config = load_test_config()
    agent = ArcAgent(config)
    await agent.startup()
    assert agent.capabilities()

def test_my_function():
    result = my_function("input")
    assert result == "expected"
```

---

## Pull Request Process

### Before Submitting

```bash
# Run all checks
uv run pre-commit run --all-files

# Run tests
uv run pytest tests/

# Update documentation
# If changing public API, update docs/
```

### PR Checklist

- [ ] Tests pass (`uv run pytest`)
- [ ] Type check passes (`uv run mypy --strict`)
- [ ] Lint passes (`uv run ruff check`)
- [ ] Documentation updated
- [ ] Changelog updated (if applicable)

---

## Release Process

### Version Bump

```bash
# Update version in all packages
# packages/*/pyproject.toml

# Create tag
git tag v1.0.0
git push origin v1.0.0

# Build packages
uv build packages/*/
```

### Publishing

```bash
# Test PyPI
uv publish --publish-url https://test.pypi.org/legacy/

# Production PyPI
uv publish
```

---

## Documentation

### Documentation Structure

```
docs/
├── 01-14-*.md           # Core documentation
├── packages/            # Package-specific docs
├── SETUP.md             # Installation
├── QUICKSTART.md        # Quick start
├── BLUEPRINTS.md        # Blueprints
├── SECURITY.md          # Security
├── DATA_FLOW.md         # Data flow
├── API_REFERENCE.md     # API
├── IMPLEMENTATION_GUIDES.md  # Guides
├── TIERS_AND_PRESETS.md  # Tiers
├── PACKAGE_INDEX.md     # Package index
├── TROUBLESHOOTING.md   # Troubleshooting
├── DEPLOYMENT.md        # Deployment
├── CONTRIBUTING.md      # This file
├── TESTING.md           # Testing
└── PERFORMANCE.md       # Performance
```

### Adding Documentation

1. Create markdown file in `docs/`
2. Add to `docs/README.md` navigation
3. Include mermaid diagrams for architecture
4. Include code examples

---

## Development Commands

```bash
# Run specific package
uv run --package arcagent pytest packages/arcagent/tests/

# Run specific test
uv run pytest packages/arcagent/tests/test_agent.py::test_startup

# Type check specific package
uv run mypy packages/arctrust

# Lint specific package
uv run ruff check packages/arctrust

# Format specific package
uv run ruff format packages/arctrust
```

---

## Next Steps

- [TESTING](TESTING.md) - Testing guidelines
- [PERFORMANCE](PERFORMANCE.md) - Performance optimization