# Arc Documentation

> **New here?** Start with [Section 1: Quickstart and Basics](QUICKSTART.md). It assumes nothing.
> **Need to ship a change today?** Jump to [Section 3: Reference](CONTRIBUTING.md).
> **Lost in a term?** Keep [Glossary](GLOSSARY.md) open in a second tab.

---

## Documentation Structure

This manual is organized into three sections:

| Section | Content | Documents |
|---|---|---|
| **1. Quickstart and Basics** | Get started, understand what Arc is, install and run your first agent | QUICKSTART, SETUP, BLUEPRINTS, TIERS_AND_PRESETS |
| **2. System Walkthroughs** | Deep dive into how Arc works, layer by layer | Comprehensive docs with diagrams and architecture |
| **3. Reference** | Detailed API, package docs, troubleshooting, deployment guides | API_REFERENCE, PACKAGE_INDEX, CONTRIBUTING, TESTING, DEPLOYMENT, etc. |

---

## Section 1: Quickstart and Basics

| Document | Purpose |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | Create and run your first agent in 5 minutes |
| [SETUP.md](SETUP.md) | Installation, configuration, environment setup |
| [BLUEPRINTS.md](BLUEPRINTS.md) | Use and create signed agent presets |
| [TIERS_AND_PRESETS.md](TIERS_AND_PRESETS.md) | Security tiers and configuration presets |

---

## Section 2: System Walkthroughs

### Orientation

| Document | What it answers |
|---|---|
| [SETUP.md](SETUP.md) | What Arc is, who it's for, what an agent actually does? Plain language. |
| [PACKAGE_INDEX.md](PACKAGE_INDEX.md) | Which package do I change? What are the layering laws? |
| [DATA_FLOW.md](DATA_FLOW.md) | One message, traced through every layer, naming every real function. |

### The Engine

| Document | What it answers |
|---|---|
| [DATA_FLOW.md](DATA_FLOW.md) | One interface, every provider, zero vendor SDKs. How `arcllm` works. |
| [API_REFERENCE.md](API_REFERENCE.md) | The loop, the strategies, steering a run in flight, budgets, sandboxes. |
| [BLUEPRINTS.md](BLUEPRINTS.md) | Where the agent's instructions and abilities actually come from. |
| [DATA_FLOW.md](DATA_FLOW.md) | How information gets in, gets extracted, and comes back later. |

### The Record

| Document | What it answers |
|---|---|
| [DATA_FLOW.md](DATA_FLOW.md) | Every byte Arc writes: where, what format, who reads it. |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Every recurring end-to-end flow beyond a single chat turn. |
| [SECURITY.md](SECURITY.md) | Identity, signing, authorization, audit, tiers, the trifecta gate. |

### Building on Arc

| Document | What it answers |
|---|---|
| [IMPLEMENTATION_GUIDES.md](IMPLEMENTATION_GUIDES.md) | Every seam you can hook into without forking the core. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, quality gates, house rules, how work lands. |
| [GLOSSARY.md](GLOSSARY.md) | Every Arc term, defined for a non-expert. |

---

## Section 3: Reference

### Technical Reference

| Document | Coverage |
|---|---|
| [API_REFERENCE.md](API_REFERENCE.md) | All classes, methods, functions with signatures |
| [PACKAGE_INDEX.md](PACKAGE_INDEX.md) | All 18 packages with detailed documentation |
| [IMPLEMENTATION_GUIDES.md](IMPLEMENTATION_GUIDES.md) | How-to guides for common tasks |
| [DATA_FLOW.md](DATA_FLOW.md) | Architecture diagrams, turn flow, memory system |
| [SECURITY.md](SECURITY.md) | Four pillars, tiers, lethal trifecta, compliance |
| [DIAGRAMS.md](DIAGRAMS.md) | Index of all Mermaid architecture diagrams |

### Operations

| Document | Coverage |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | Production deployment, Docker, Kubernetes, air-gapped setups |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues, diagnostic commands, error resolution |
| [PERFORMANCE.md](PERFORMANCE.md) | Performance tuning, scaling, monitoring metrics |

### Development

| Document | Coverage |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, architecture rules, PR process |
| [TESTING.md](TESTING.md) | Test categories, fixtures, quality gates |
| [cli.md](cli.md) | Full `arc …` command reference |
| [config-reference.md](config-reference.md) | The config key reference |
| [prompts.md](prompts.md) | Prompt authoring and the `arcprompt` catalogue |
| [modules.md](modules.md) | Module system documentation |

### Runbooks

| Document | Coverage |
|---|---|
| [runbooks/security-hardening.md](runbooks/security-hardening.md) | Security hardening procedures |
| [runbooks/spec-017-operations.md](runbooks/spec-017-operations.md) | Operations runbook |
| [runbooks/tasks-module.md](runbooks/tasks-module.md) | Task system documentation |
| [runbooks/agent-level-features.md](runbooks/agent-level-features.md) | Agent-level features |
| [runbooks/security/threat-model.md](runbooks/security/threat-model.md) | Threat model |
| [runbooks/security/adversarial-tests.md](runbooks/security/adversarial-tests.md) | Adversarial testing |
| [runbooks/security/nist-800-53-mapping.md](runbooks/security/nist-800-53-mapping.md) | NIST compliance mapping |

### Additional Guides

| Document | Coverage |
|---|---|
| [azure-openai-setup.md](azure-openai-setup.md) | Azure OpenAI provider setup |
| [firecracker-deployment.md](firecracker-deployment.md) | Firecracker microVM deployment |
| [trust-model.md](trust-model.md) | Trust model documentation |
| [voice-air-gap-setup.md](voice-air-gap-setup.md) | Air-gapped voice setup |

### Architecture Decisions

| Document | Coverage |
|---|---|
| [architecture/ARCH-OVERVIEW.md](architecture/ARCH-OVERVIEW.md) | Original architecture overview |
| [architecture/decisions/](architecture/decisions/) | Architecture Decision Records (ADRs 001-029) |
| [architecture/policy-modules.md](architecture/policy-modules.md) | Policy module catalogue |

---

## Reading Paths

Pick the path that matches why you're here:

```mermaid
flowchart TB
    classDef entry   fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef agent   fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef llm     fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found   fill:#002550,stroke:#001A38,color:#FFFFFF

    START["Why are you here?"]

    START --> A["I want to understand<br/>what Arc is"]
    START --> B["I'm writing code<br/>this week"]
    START --> C["I'm evaluating Arc<br/>for security"]
    START --> D["I'm adding a<br/>capability"]
    START --> E["I need to deploy<br/>to production"]

    A --> A1["QUICKSTART.md"] --> A2["SETUP.md"] --> A3["GLOSSARY.md"]
    A --> A4["PACKAGE_INDEX.md"] --> A5["DATA_FLOW.md"]
    B --> B1["DATA_FLOW.md"] --> B2["PACKAGE_INDEX.md"] --> B3["CONTRIBUTING.md"]
    B --> B4["API_REFERENCE.md"] --> B5["IMPLEMENTATION_GUIDES.md"]
    C --> C1["SECURITY.md"] --> C2["TIERS_AND_PRESETS.md"] --> C3["DATA_FLOW.md"]
    D --> D1["IMPLEMENTATION_GUIDES.md"] --> D2["BLUEPRINTS.md"] --> D3["API_REFERENCE.md"]
    E --> E1["DEPLOYMENT.md"] --> E2["TROUBLESHOOTING.md"] --> E3["PERFORMANCE.md"]

    class START entry
    class A,B,C,D,E surface
    class A1,A2,A3,A4,A5 agent
    class B1,B2,B3,B4,B5 runtime
    class C1,C2,C3 found
    class D1,D2,D3 llm
    class E1,E2,E3 llm
```

| I am… | Read, in order |
|---|---|
| **New to Arc entirely** | [QUICKSTART.md](QUICKSTART.md) → [SETUP.md](SETUP.md) → [PACKAGE_INDEX.md](PACKAGE_INDEX.md) |
| **A new contributor with a task** | [DATA_FLOW.md](DATA_FLOW.md) → [PACKAGE_INDEX.md](PACKAGE_INDEX.md) → [CONTRIBUTING.md](CONTRIBUTING.md) → [API_REFERENCE.md](API_REFERENCE.md) |
| **Non-technical or semi-technical** | [QUICKSTART.md](QUICKSTART.md) → [GLOSSARY.md](GLOSSARY.md) → [SETUP.md](SETUP.md) |
| **A security reviewer** | [SECURITY.md](SECURITY.md) → [TIERS_AND_PRESETS.md](TIERS_AND_PRESETS.md) → [DATA_FLOW.md](DATA_FLOW.md) |
| **Extending Arc without forking** | [IMPLEMENTATION_GUIDES.md](IMPLEMENTATION_GUIDES.md) → [BLUEPRINTS.md](BLUEPRINTS.md) → [API_REFERENCE.md](API_REFERENCE.md) |
| **Operating a deployed fleet** | [DATA_FLOW.md](DATA_FLOW.md) → [DEPLOYMENT.md](DEPLOYMENT.md) → [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

---

## Document Cross-Reference Map

The comprehensive docs work together as an integrated manual:

| Topic | Primary Docs |
|-------|--------------|
| **Getting Started** | [QUICKSTART.md](QUICKSTART.md), [SETUP.md](SETUP.md), [GLOSSARY.md](GLOSSARY.md) |
| **Architecture** | [PACKAGE_INDEX.md](PACKAGE_INDEX.md), [DATA_FLOW.md](DATA_FLOW.md) |
| **Security** | [SECURITY.md](SECURITY.md), [TIERS_AND_PRESETS.md](TIERS_AND_PRESETS.md) |
| **API Reference** | [API_REFERENCE.md](API_REFERENCE.md) |
| **Implementation** | [IMPLEMENTATION_GUIDES.md](IMPLEMENTATION_GUIDES.md), [BLUEPRINTS.md](BLUEPRINTS.md) |
| **Deployment** | [DEPLOYMENT.md](DEPLOYMENT.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md), [PERFORMANCE.md](PERFORMANCE.md) |
| **Development** | [CONTRIBUTING.md](CONTRIBUTING.md), [TESTING.md](TESTING.md) |

---

## Conventions

- **Every claim points at code.** Locations are written `path/to/file.py:120` so they're clickable.
- **Built ≠ wired.** A correct predicate shipping with dead activating wiring is called out explicitly.
- **Diagrams are Mermaid**, rendered inline by GitHub and most editors, using a shared color palette.
- **House rules** live in `../CLAUDE.md` — build standards, four pillars, quality gates.