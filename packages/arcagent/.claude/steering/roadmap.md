# Product Roadmap

> This document provides implementation planning context that informs feature execution.
> Feature-specific tasks go in `.claude/specs/{feature}/PLAN.md` documents.

## Validation Checklist

- [x] Current phase defined
- [x] Phase goals clear
- [x] Dependencies mapped
- [x] Parallel work identified
- [x] Success criteria defined
- [x] No [NEEDS CLARIFICATION] markers

---

## Roadmap Overview

### Current Phase

**Phase**: Phase 1b - Agent Runtime
**Timeline**: Weeks 3-6
**Focus**: Skills, extensions, session persistence, additional tools, CLI integration

### Phase Summary

| Phase | Name | Focus | Status |
|-------|------|-------|--------|
| 1a | Core Nucleus | Core 7 components, built-in tools, Module Bus | Complete (S001) |
| 1b | Agent Runtime | Skills, extensions, session persistence, compaction, additional tools, CLI wiring | Current |
| 2 | Security Hardening | Vault, mTLS, PKI identity, policy engine, input/output classification, module signing | Planned |
| 3 | Scale Infrastructure | NATS JetStream, orchestrator, Firecracker, Temporal.io, agent pools | Planned |
| 4 | Ecosystem | OpenClaw adapter, module marketplace, specialized agents, evaluation framework | Planned |
| 5 | Advanced | Multi-agent coordination, learning module, graph memory, voice/multimodal, FedRAMP prep | Planned |

---

## Implementation Philosophy

### Specification Compliance

> All implementation must follow approved specifications.

#### Before Each Feature

1. Read feature specification in `.claude/specs/{feature}/`
2. Verify PRD requirements understood
3. Confirm SDD design decisions
4. Load PLAN tasks into TodoWrite

#### Deviation Protocol

If implementation cannot follow spec exactly:

1. **Document** the deviation and reason
2. **Get approval** before proceeding
3. **Update spec** if deviation is an improvement
4. **Never deviate** without documentation

### TDD Approach

> Each feature follows Test-Driven Development.

```
For each task:
1. Prime Context - Read relevant specs and patterns
2. Write Tests - Define expected behavior
3. Implement - Make tests pass
4. Validate - Run quality checks
```

---

## Current Phase Details

### Phase 1a: Core Nucleus — COMPLETE

> Spec: S001-phase1-core. 141 tests, 95.97% line coverage, 85.58% branch coverage.
> Core LOC: 1,509 total / 1,155 code-only (budget: 3,000).

**Completed components:**

| Component | File | Status |
|-----------|------|--------|
| Config parser (TOML + Pydantic) | `core/config.py` | Complete |
| Identity (Ed25519 DID, sign/verify) | `core/identity.py` | Complete |
| Telemetry (OTel spans + audit) | `core/telemetry.py` | Complete |
| Module Bus (async events, priority, veto) | `core/module_bus.py` | Complete |
| Tool Registry (register, wrap, policy) | `core/tool_registry.py` | Complete |
| Context Manager (prompt assembly, pruning) | `core/context_manager.py` | Complete |
| Agent orchestrator (wires all components) | `core/agent.py` | Complete |
| Built-in tools (read, write, edit, bash) | `tools/` | Complete |

---

### Phase 1b: Agent Runtime (Current)

#### Goals

- [ ] Extension system (Python lifecycle hooks, tool registration, discovery)
- [ ] Skill registry (SKILL.md discovery, progressive disclosure in prompt)
- [ ] Additional tools (grep, find, ls)
- [ ] Session persistence (JSONL transcripts, resume)
- [ ] LLM-based compaction (Letta-style sliding window, pre-compaction flush)
- [ ] Wire arccli through ArcAgent (not direct arcrun)
- [ ] Settings manager (runtime-configurable agent behavior)
- [ ] Markdown memory module (S002 — entities, notes, context.md)

#### Features in Scope

| Feature | Priority | Dependencies | Spec | Status |
|---------|----------|-------------|------|--------|
| Extension system | P0 | Tool Registry, Module Bus | Needs spec (v3 design §8) | Not Started |
| Skill registry | P0 | Context Manager | Needs spec (v3 design §5) | Not Started |
| Additional tools (grep, find, ls) | P0 | Tool Registry | None needed | Not Started |
| Session persistence (JSONL) | P0 | None | S002 (SessionManager) | Not Started |
| LLM-based compaction | P0 | Session persistence | S002 (compact + flush) | Not Started |
| Wire arccli through ArcAgent | P0 | All above | See CLI commands list | Not Started |
| Settings manager | P1 | Config | Needs spec | Not Started |
| Markdown memory module | P1 | Module Bus, Context Manager | S002 | Not Started |
| Container isolation (Docker) | P2 | CLI | Needs spec | Not Started |

#### Extension System Design (from v3 doc §8)

```python
class ArcAgentExtension(Protocol):
    name: str
    version: str

    # Lifecycle hooks
    async def on_agent_start(self, agent: AgentContext) -> None: ...
    async def on_agent_stop(self, agent: AgentContext) -> None: ...
    async def on_session_start(self, session: SessionContext) -> None: ...
    async def on_session_end(self, session: SessionContext) -> None: ...

    # Tool/turn hooks
    async def before_tool_call(self, call: ToolCall) -> ToolCall | None: ...
    async def after_tool_call(self, call: ToolCall, result: ToolResult) -> ToolResult: ...
    async def before_llm_call(self, messages: list[Message]) -> list[Message]: ...
    async def after_llm_response(self, response: LLMResponse) -> LLMResponse: ...

    # Registration
    tools: list[ToolDefinition]        # Tools this extension provides
    memory_provider: MemoryProvider | None
    skills: list[str]                  # SKILL.md files this extension bundles
```

**Discovery locations:**
1. `~/.arcagent/extensions/` (global)
2. `workspace/extensions/` (per-agent)
3. Config-specified paths

**Loading mechanism:** `importlib.import_module()` — Python files export a factory function
that receives an API object with `register_tool()`, `on()`, etc.

**Hot reload:** `/reload` command re-runs discovery + loading pipeline without restart.

#### Skill Registry Design (from v3 doc §5)

```python
class SkillRegistry:
    def discover(self, *dirs: Path) -> list[SkillMeta]:
        """Scan dirs for SKILL.md files, parse YAML frontmatter (name, description)."""

    def format_for_prompt(self) -> str:
        """Return XML with name + description + path for progressive disclosure."""
```

**Discovery locations:**
1. `workspace/skills/` (per-agent, including `_agent-created/`)
2. `~/.arcagent/skills/` (global)

**Progressive disclosure:** Only name + description in system prompt. Agent uses `read` tool
to load full SKILL.md content when relevant. Scales to hundreds of skills.

#### Dependencies

```
Phase 1a (Complete) ──┬──▶ Extension System ──▶ Hot Reload
                      ├──▶ Skill Registry
                      ├──▶ Additional Tools (grep, find, ls)
                      ├──▶ Session Persistence ──▶ Compaction
                      ├──▶ Settings Manager
                      └──▶ Memory Module (S002)
                                │
                      All above ▼
                      CLI Integration (arccli wiring)
```

| Feature | Depends On | Blocks |
|---------|------------|--------|
| Extension system | Phase 1a complete | Hot reload, CLI |
| Skill registry | Phase 1a complete | CLI |
| Additional tools | Phase 1a complete | CLI |
| Session persistence | Phase 1a complete | Compaction, CLI |
| Compaction | Session persistence | CLI |
| Settings manager | Config | CLI |
| Memory module (S002) | Module Bus, Context Manager | CLI (optional) |
| CLI integration | All above | Container isolation |

#### Parallel Opportunities

| Stream 1 (Self-Extension) | Stream 2 (Session/Memory) | Stream 3 (Tools + Settings) |
|--------------------------|--------------------------|----------------------------|
| Extension system | Session persistence (JSONL) | Additional tools (grep/find/ls) |
| Skill registry | LLM-based compaction | Settings manager |
| Hot reload mechanism | Memory module (S002) | -- |
| -- | -- | CLI wiring (after all above) |

#### Out of Scope (Phase 1b)

- Vault integration (Phase 2)
- mTLS (Phase 2)
- Policy engine (Phase 2)
- Module signing (Phase 2)
- NATS messaging (Phase 3)
- Firecracker (Phase 3)

#### Key Insight: Auto-Retry Already Handled

arcllm has a built-in `RetryModule` with exponential backoff + jitter on transient failures
(429, 500, 502, 503, 529). Enabled via `arcllm.load_model(..., retry=True)`. No need to
reimplement at the arcagent layer.

---

### Future: Agent Bootstrap System (needs `/brainstorm`)

> Concept identified during S002 gap analysis. Not yet specified — needs brainstorm session.

**Idea**: A bootstrap process that runs when an agent workspace is first created. Populates initial workspace files (identity.md, policy.md, context.md, notes directory, entities directory) from templates or interactive prompts. Ensures agents start with a functional memory workspace rather than empty directories.

**Open questions** (for `/brainstorm`):
- Interactive vs template-based vs hybrid bootstrap?
- Does bootstrap populate identity.md from config, or ask the user?
- Should bootstrap seed policy.md with domain-specific starter rules?
- How does bootstrap interact with container isolation (Phase 1 Docker)?
- Should bootstrap be a CLI command (`arcagent init`) or automatic on first `run()`?

---

## Phase 2: Security Hardening (Weeks 5-8)

#### Goals

- [ ] Vault integration for credential management
- [ ] mTLS for all internal communications
- [ ] Agent identity via PKI (cert-per-agent, challenge-response auth)
- [ ] Tool policy engine (allowlist, blocked patterns, approval workflows)
- [ ] Input classification module (trusted/untrusted/hostile)
- [ ] Output classification (PII/CUI detection)
- [ ] Lethal Trifecta breaker (approval gates)
- [ ] Module signing and provenance verification
- [ ] Federal policy module (FedRAMP, NIST 800-53 controls)

---

## Phase 3: Scale Infrastructure (Weeks 9-12)

#### Goals

- [ ] NATS JetStream for inter-agent messaging
- [ ] Agent orchestrator (registry, scheduler, health monitor) — this is arcTeam foundation
- [ ] Firecracker microVM isolation
- [ ] Temporal.io workflow integration
- [ ] Agent pool management (spawn, scale, destroy)
- [ ] Distributed config (etcd)
- [ ] Load testing: 100 -> 1,000 -> 10,000 agents

---

## Phase 4: Ecosystem (Weeks 13-16)

#### Goals

- [ ] OpenClaw skill adapter (import community skills with sandboxing)
- [ ] Module marketplace infrastructure
- [ ] Specialized agent templates (coding, design, procurement)
- [ ] Evaluation framework (accuracy, cost, latency scoring)
- [ ] Self-improvement loop (policy.md system, agent proposes improvements)
- [ ] Documentation + onboarding for CTG Federal / BlackArc customers
- [ ] SBOM generation for all deployments

---

## Phase 5: Advanced Capabilities (Weeks 17+)

#### Goals

- [ ] Multi-agent coordination patterns (swarm, hierarchy, consensus)
- [ ] Agent-to-agent handoff protocols
- [ ] Learning module (agents improve from experience via policy.md)
- [ ] Graph memory module (temporal knowledge graphs)
- [ ] Voice/multimodal channel modules
- [ ] FedRAMP authorization package preparation
- [ ] arcTeam shared business context (semantic layer for fleet)

---

## Task Execution Framework

### Task Metadata Tags

> Used in PLAN.md files for agent orchestration.

| Tag | Purpose | Example |
|-----|---------|---------|
| `[parallel: true]` | Can run concurrently | Config + Module Bus in parallel |
| `[component: name]` | Component grouping | `[component: IdentityService]` |
| `[ref: doc/section]` | Specification reference | `[ref: arcagent-plan.md/3.6]` |
| `[activity: type]` | Agent selection hint | `[activity: core-development]` |
| `[blocked-by: task]` | Dependency | `[blocked-by: T1.1]` |

### Task States

| State | Meaning | Next Action |
|-------|---------|-------------|
| `[ ]` | Not started | Begin when dependencies complete |
| `[~]` | In progress | Continue or hand off |
| `[x]` | Complete | Validate, move to next |
| `[!]` | Blocked | Resolve blocker |
| `[-]` | Skipped | Document reason |

### Standard Task Template

```markdown
- [ ] **T{phase}.{number}** {Task Name} `[activity: type]`
  - [ ] T{phase}.{number}.1 {Subtask 1}
  - [ ] T{phase}.{number}.2 {Subtask 2}
  - _Requirements: {reference}_
  - _Design: {reference}_
```

---

## Success Criteria Framework

### Automated Verification

> Run these before considering any feature complete.

```bash
# All tests pass
pytest

# Coverage meets threshold
pytest --cov=arcagent --cov-report=term-missing  # >=80% line, >=75% branch

# No linting errors
ruff check .

# No type errors
mypy arcagent/

# Build succeeds (package builds cleanly)
python -m build

# No security vulnerabilities
pip-audit
```

### Manual Verification

| Category | Criteria | Reviewer |
|----------|----------|----------|
| Architecture | Core stays under 3K LOC | Architecture review |
| Security | Identity flow is cryptographically sound | Security review |
| Functionality | Agent loop completes end-to-end | Developer |
| Documentation | Module development guide is clear | External reviewer |

---

## Phase Transition Checklist

### Completing a Phase

- [ ] All phase features implemented
- [ ] All automated tests passing (>=80% coverage)
- [ ] All manual verification complete
- [ ] Documentation updated
- [ ] Specs marked complete
- [ ] Core LOC count verified (< 3,000)
- [ ] Security review conducted (Phase 2+)
- [ ] Load test results documented (Phase 3+)

### Starting a Phase

- [ ] Previous phase complete
- [ ] Phase goals reviewed
- [ ] Dependencies available (ArcLLM, ArcRun, Vault, NATS, etc.)
- [ ] Specs approved (PRD -> SDD -> PLAN)
- [ ] Environment ready (dev tooling, test infrastructure)

---

## Risk Register

### Active Risks

| Risk | Impact | Likelihood | Mitigation | Phase |
|------|--------|------------|------------|-------|
| Core exceeds 3K LOC | H | M | Strict boundaries, push to modules | Phase 1 |
| ArcLLM/ArcRun not ready | H | L | Build interface first, mock implementation | Phase 1 |
| Vault complexity for dev mode | M | M | File-based fallback for Phase 1 | Phase 1 |
| Firecracker setup complexity | M | M | Docker first, Firecracker in Phase 3 | Phase 3 |
| OpenClaw skill adapter scope creep | M | H | Limit to SKILL.md parsing, sandbox everything | Phase 4 |
| FedRAMP timeline | H | M | Start authorization package in Phase 2, parallel track | Phase 5 |

---

## Milestone Tracking

### Phase 1a Milestones (Complete)

| Milestone | Criteria | Status |
|-----------|----------|--------|
| M1a.1: Config + Module Bus | Config parses, Module Bus emits events | Complete |
| M1a.2: Core Components | Identity, Telemetry, Context Manager, Tool Registry working | Complete |
| M1a.3: Agent Loop | Full think-act-observe cycle with mock LLM | Complete |
| M1a.4: Built-in Tools | read, write, edit, bash with workspace boundary enforcement | Complete |

### Phase 1b Milestones

| Milestone | Criteria | Status |
|-----------|----------|--------|
| M1b.1: Additional Tools | grep, find, ls tools working with workspace validation | Not Started |
| M1b.2: Skill Registry | SKILL.md discovery, YAML frontmatter parsing, prompt injection | Not Started |
| M1b.3: Extension System | Discovery, loading via importlib, registerTool API, hot reload | Not Started |
| M1b.4: Session Persistence | JSONL transcripts, create/append/resume/compact | Not Started |
| M1b.5: Compaction | Letta-style sliding window, pre-compaction flush to context.md | Not Started |
| M1b.6: Settings Manager | Runtime-configurable agent behavior (model, compaction, tools) | Not Started |
| M1b.7: CLI Integration | arccli wired through ArcAgent (not direct arcrun) | Not Started |

### Status Legend

- Not Started
- In Progress
- At Risk
- Blocked
- Complete

---

## References

- Architecture plan: `arcagent-plan.md`
- Design document: `arcagent-design-v3.md`
- Enterprise mesh reference: `../enterprise-mesh/apex-core/`
