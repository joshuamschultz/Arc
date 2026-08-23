# Tech Steering

> Stable technical context referenced by feature specs in `.codex/specs/`.
> Downstream consumers: `spec-sdd-generator` (tech stack, conventions).

## Tech Stack

> Languages, frameworks, runtimes. Cite exact versions where they matter.

| Layer | Technology | Version | Notes |
|-------|------------|---------|-------|
| Language | [NEEDS CLARIFICATION: e.g., TypeScript] | [Version] | [Strict mode etc.] |
| Framework | [e.g., Next.js] | [Version] | [Router choice] |
| Database | [e.g., PostgreSQL] | [Version] | [Hosting] |
| Auth | [e.g., Supabase Auth] | | |
| Styling | [e.g., Tailwind] | [Version] | |
| AI Orchestration | [LangGraph / LangChain / none] | [Version] | [Drives $schultz-gpt:workflow-implement AI-task routing] |
| Internal Frameworks | [name → owning agent, or _none_] | | [e.g., `arc → agentic-framework-architect`] |

## Conventions

> Code-style, naming, and review conventions every feature must follow.

### Naming

| Item | Convention | Example |
|------|------------|---------|
| Components | PascalCase | `UserProfile.tsx` |
| Hooks | `useCamelCase` | `useUserProfile.ts` |
| Utilities | camelCase | `formatDate.ts` |
| DB tables | snake_case, plural | `user_profiles` |

### Style

- Linter: [NEEDS CLARIFICATION: e.g., ESLint with X config]
- Formatter: [e.g., Prettier]
- Type-check: [e.g., `tsc --noEmit`]

### Review

- All PRs require [NEEDS CLARIFICATION: number] approvers.
- All PRs must pass CI before merge.

## Library Choices

> Standard libraries that every feature should reuse rather than reinvent.

| Purpose | Library | Why |
|---------|---------|-----|
| HTTP client | [NEEDS CLARIFICATION] | [Reason] |
| Validation | [e.g., Zod] | [Reason] |
| Logging | [e.g., pino] | [Reason] |
| Testing | [e.g., Vitest] | [Reason] |

## Build & CI

> Canonical commands; the CI gate; deploy targets.

### Commands

```bash
# Dev
[NEEDS CLARIFICATION: e.g., npm run dev]

# Test
[e.g., npm test]

# Lint
[e.g., npm run lint]

# Type-check
[e.g., npm run typecheck]

# Build
[e.g., npm run build]
```

### CI Gates

> These gates are the blocking thresholds `$schultz-gpt:workflow-review` enforces. The Line coverage row
> drives `coverage_threshold` (default 80 if this row is absent).

| Gate | Threshold |
|------|-----------|
| Line coverage | ≥80% |
| Lint | 0 errors |
| Type errors | 0 |
| Critical vulns | 0 |

## Observability

> How features emit logs, metrics, traces.

| Signal | Tool | Convention |
|--------|------|------------|
| Logs | [NEEDS CLARIFICATION] | Structured JSON |
| Metrics | [e.g., Prometheus] | Per-endpoint p95 |
| Traces | [e.g., OpenTelemetry] | Request-scoped |
| Errors | [e.g., Sentry] | Captured server + client |

## Compliance

> The compliance regime that binds every feature. `$schultz-gpt:workflow-build` and `principled-coder`
> read this section: a declared regime's mandated controls auto-apply (never asked);
> with `regime: none`, only the OWASP baseline auto-applies.

```
regime: none | soc2 | hipaa | fedramp/nist | <custom>
```

> If a regime is declared, list the mandated controls it enforces (optional):

| Control | Requirement | Source |
|---------|-------------|--------|
| [e.g., Encryption at rest] | [AES-256 / FIPS 140-3] | [Regime clause] |
| [e.g., Audit log retention] | [≥1 year] | [Regime clause] |

> Machine-readable mandates (optional): point `$schultz-gpt:workflow-build` at a project-local
> `.codex/steering/compliance-mandates.json` (schema in the plugin's
> `assets/compliance-mandates.example.json`). Absent file = no extra mandates.
