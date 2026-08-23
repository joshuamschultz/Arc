# Structure Steering

> Stable architectural context referenced by feature specs in `.codex/specs/`.
> Downstream consumers: `spec-sdd-generator` (layer model, module boundaries).

## Layer Model

> The architectural layers and what each owns.

| Layer | Responsibility | Examples |
|-------|---------------|----------|
| Presentation | UI rendering, user interaction | Components, pages |
| Application | Orchestration, state | Hooks, server actions |
| Domain | Business logic, rules | Services, entities |
| Infrastructure | External I/O | DB, auth, third-party APIs |

[NEEDS CLARIFICATION: Adjust the layer set for this project's actual architecture.]

## Folder Layout

> The canonical directory structure. Every feature follows this.

```
[NEEDS CLARIFICATION: Replace with actual folder layout.]

Example:
/
├── src/
│   ├── app/                    # Routes
│   ├── components/             # UI components
│   ├── lib/                    # Shared logic
│   └── types/                  # Type definitions
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── .codex/
    ├── steering/               # This folder
    └── specs/                  # Feature specs
```

## Naming Rules

> Names that must remain stable across features.

| Item | Convention | Example |
|------|------------|---------|
| Feature folder | kebab-case | `feature-search/` |
| Component file | PascalCase | `SearchResults.tsx` |
| API route file | kebab-case | `search-results/route.ts` |
| Test file | mirror source + `.test` | `SearchResults.test.tsx` |

## Dependency Direction

> Which layers may depend on which.

```
Presentation → Application → Domain → Infrastructure
```

Rules:
- Lower layers MUST NOT import from higher layers.
- Domain MUST NOT import frameworks (keep it pure).
- Infrastructure adapters MAY be swapped without touching Domain.

[NEEDS CLARIFICATION: Adjust dependency rules for this project.]

## Module Boundaries

> The domain boundaries that must remain independently deployable / refactorable.
> `principled-coder` and `$schultz-gpt:workflow-implement` read this table: it is the authoritative module
> map for splitting `mixed`-domain tasks and for enforcing the Modularity pillar.

| Module | Path | Responsibility (one-line contract) | Public API | Forbidden Imports |
|--------|------|------------------------------------|------------|-------------------|
| [NEEDS CLARIFICATION: e.g., auth] | `lib/auth/` | Auth flows and sessions | `lib/auth/index.ts` | UI frameworks |
| [e.g., billing] | `lib/billing/` | Subscription lifecycle | `lib/billing/index.ts` | Auth internals |
| [e.g., search] | `lib/search/` | Search index and query | `lib/search/index.ts` | Billing |
