## Stack & Quality Gates

### Foundation packages

| Package | Purpose |
|---------|---------|
| `arcllm` (`packages/arcllm`) | Provider-agnostic LLM calls |
| `arcrun` (`packages/arcrun`) | Runtime agentic loop |
| `arctrust` | Identity, sign, authorize, audit (leaf) |

### Key libraries

| Library | Purpose |
|---------|---------|
| Pydantic 2.x | Data validation, config schemas |
| PyNaCl | Ed25519 cryptography |
| OpenTelemetry SDK | Traces, metrics, audit |
| NATS.py | Message bus |
| httpx | Async HTTP |
| uvloop | High-performance event loop |

### Commands

```bash
ruff check .                    # Lint
ruff format .                   # Format
mypy arcagent/ --strict         # Type check
pytest --cov=arcagent           # Test + coverage
pip-audit                       # Dependency audit
```

### Gates

| Gate | Threshold |
|------|-----------|
| Line coverage | ≥ 80% |
| Branch coverage | ≥ 75% |
| Core component coverage | ≥ 90% |
| Cyclomatic complexity | ≤ 10 per function |
| Ruff errors | 0 |
| mypy errors | 0 |
| Critical/high vulnerabilities | 0 |
| Core LOC | < 5,000 |