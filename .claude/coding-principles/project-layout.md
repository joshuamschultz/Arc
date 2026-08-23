## Project Structure

```
arc/
├── packages/                   # Core packages (alphabetical)
│   ├── arcbundle/             # Signed module bundles
│   ├── arccli/                # Command-line interface (`arc` command)
│   ├── arcgateway/            # Chat platform gateway (Telegram, Slack, etc.)
│   ├── arcllm/                # Provider-agnostic LLM calls (17 providers)
│   ├── arcmas/                # Meta-package (pip install arcmas = full stack)
│   ├── arcmemory/             # Dual-speed analogical memory
│   ├── arcmodel/              # Model management and routing (scaffolding)
│   ├── arcokf/                # Typed document contract for knowledge
│   ├── arcprompt/             # Editable, signed system prompts
│   ├── arcrun/                # Runtime agentic loop (ReAct engine)
│   ├── arcskill/              # Verified skill hub
│   ├── arcstore/              # Durable storage foundation
│   ├── arcteam/               # Multi-agent coordination
│   ├── arctrust/              # Cryptographic foundation (leaf package)
│   ├── arctui/                # Terminal UI
│   └── arcui/                 # Real-time web dashboard
├── arcagent/                  # Main agent package (src/arcagent/)
│   ├── core/                  # Nucleus
│   │   ├── agent.py           # Orchestrator
│   │   ├── config.py          # TOML config, Pydantic validation
│   │   ├── telemetry.py       # OpenTelemetry, audit events
│   │   ├── tool_registry.py   # Tool registry, 4 transports
│   │   ├── module_bus.py      # Event-driven extensions
│   │   ├── session_internal/  # Session management
│   │   └── vault/             # Vault integration
│   ├── brain/                 # Brain modules
│   ├── builtins/              # Built-in capabilities
│   ├── capabilities/          # Capability definitions
│   ├── extension/             # Extension system
│   ├── modules/               # Official modules
│   ├── orchestration/         # Orchestration logic
│   ├── skilladapt/            # Skill adaptation
│   ├── tools/                 # Agent tools
│   └── utils/                 # Shared utilities
├── evaluations/               # Evaluation suites
│   ├── ingest/                # Data ingestion evals
│   └── longmemeval/           # Long memory evaluation
├── docs/                      # Documentation
│   ├── building/              # Build standards
│   ├── concepts/              # Design concepts
│   ├── walkthrough/           # User guides
│   └── runbooks/              # Operational procedures
├── scripts/                   # Utility scripts
├── tests/                     # Test suite
│   ├── unit/                  # 60%
│   ├── integration/           # 20%
│   ├── e2e/                   # 10%
│   ├── security/              # 5%
│   └── performance/           # 5%
└── tools/                     # Development tools
```

## Code Standards

### Readability

- Clean, readable code is non-negotiable.
- No complex inner loops with buried logic — break them out.
- Methods short enough to read without scrolling.
- Names communicate intent: `validate_module_signature`, not `check`.
- Comment the **why**, not the **what**.

### Abstractions

- DRY: extract shared patterns into base classes and utilities.
- Don't abstract prematurely — **three instances** before extracting.
- Abstractions must reduce cognitive load, not add it.
- Every abstraction needs a clear interface (`Protocol` or ABC).

### Maintainability

- Modular: a change in one component should not ripple across the codebase.
- Strong typing everywhere — `mypy --strict` must pass.
- Pydantic models for all data boundaries (config, messages, events).
- Depend on protocols, not concrete classes.
- Feature toggles via config, not code branches.