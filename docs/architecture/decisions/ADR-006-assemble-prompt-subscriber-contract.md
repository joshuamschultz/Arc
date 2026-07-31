# ADR-006: Canonical Contract for `agent:assemble_prompt` Subscribers

**Status**: Accepted
**Date**: 2026-03-01
**Decision Makers**: Josh Schultz
**Relates to**: SPEC-013 Convention-Driven Prompt Injection, ADR-004 Core LOC Budget

---

## Context

Multiple modules subscribe to the `agent:assemble_prompt` event to inject sections into the system prompt. Today three subscribers exist:

| Module | Section Key | Priority | Pattern |
|--------|-------------|----------|---------|
| Messaging | `teams` | 50 | XML roster + markdown rules |
| Tool Registry | `tools` | 85 | XML catalog |
| Skill Registry | `skills` | 90 | XML catalog |

As more modules adopt this pattern, the lack of a formal contract creates risks:

1. **Section key collisions** — two modules writing to the same key silently overwrite each other.
2. **Inconsistent caching** — messaging uses TTL-based caching, tools use invalidate-on-register, skills have no caching.
3. **No validation** — there is no schema for what `sections` must look like, nor enforcement of key naming.
4. **Audit gap** — some subscribers emit audit events on rebuild, others do not.
5. **Sanitization variance** — tools/roster use `xml_escape`; messaging now uses `sanitize_text`; there is no shared requirement.

## Decision

**Define a canonical contract that all `agent:assemble_prompt` subscribers must follow.** The contract is documented here (not enforced in code) as a convention. Future work may add runtime validation.

### Section Key Convention

Section keys must be lowercase `[a-z][a-z0-9_]*` — no dots, no hyphens, no uppercase. Keys are namespaced by responsibility:

| Reserved Key | Owner Module | Purpose |
|--------------|-------------|---------|
| `teams` | messaging | Team roster, identity, communication rules |
| `tools` | tool_registry | Available tool catalog |
| `skills` | skill_registry | Available skill catalog |
| `context` | context_manager | Context window state |
| `memory` | bio_memory / markdown_memory | Recalled memories |

New modules should document their key in this ADR via amendment.

### Priority Bands

| Band | Range | Purpose |
|------|-------|---------|
| Identity | 0–49 | Agent identity, DID, roles |
| Team | 50–69 | Team coordination, roster, messaging |
| Knowledge | 70–79 | Memory, context, recalled facts |
| Tools | 80–89 | Tool catalogs |
| Skills | 90–99 | Skill catalogs |
| Extension | 100+ | Third-party extensions |

Lower priority = earlier in the prompt. Subscribers must not change priority without updating this document.

### Caching Requirements

Every subscriber that performs I/O or computation to build its section **should** cache the result:

- **Invalidate-on-write** (preferred for registries): cache cleared when the underlying registry changes. Used by tools and skills.
- **TTL-based** (for external data): cache refreshed on a configurable interval. Used by messaging roster.
- **No caching** (only acceptable for trivial sections): sections that are a few lines of static text.

Subscribers must expose a public `is_cached` property or equivalent so instrumentation can distinguish cache hits from rebuilds.

### Sanitization Requirements

All data interpolated into section content must be sanitized before assembly:

- **XML-structured content**: Use `xml_escape()` for attribute values and text content. Validate element names against NCName regex.
- **Markdown/text content**: Use `sanitize_text()` from `arcagent.utils.sanitizer` for all untrusted fields (entity names, message bodies, external identifiers).
- **Inter-agent data**: Always treated as untrusted. All fields from other agents (message sender, body, metadata) must pass through `sanitize_text()`.

### Audit Event Convention

Subscribers that rebuild their section (not cache hits) should emit an audit event:

```
prompt.<section_key>_rebuilt  →  {"entity_count": N}  or  {"tool_count": N}
```

This enables monitoring of prompt churn and helps diagnose unexpected prompt size growth.

### Error Handling

Subscriber failures must not crash prompt assembly. If a subscriber raises, the bus should:

1. Log the error at WARNING level.
2. Omit the section (prefer a shorter prompt over a broken one).
3. Emit an audit event: `prompt.<section_key>_error`.

## Alternatives Considered

### 1. Enforce contract via base class

Create an `AbstractPromptSubscriber` that validates section keys, enforces caching, and standardizes audit events. Rejected because:
- Adds coupling between modules and a new base class in core
- Convention is sufficient while subscriber count is small (<10)
- Premature abstraction (three-instance rule not yet met for all patterns)

### 2. Schema validation on sections dict

Validate the `sections` dict schema after all subscribers run. Rejected because:
- Adds per-prompt overhead for a development-time concern
- Type checking and tests provide sufficient validation

### 3. No contract — let modules diverge

Continue with ad-hoc patterns per module. Rejected because:
- Key collisions are a real risk as module count grows
- Inconsistent sanitization creates security gaps (demonstrated by SPEC-013 review)
- Audit gaps make debugging prompt issues harder

## Rationale

1. **Convention over configuration.** A documented contract is lighter than runtime enforcement and easier to evolve.
2. **Security baseline.** Mandating sanitization for all subscribers prevents the class of bugs found in the SPEC-013 pre-existing review (unsanitized entity names, message bodies).
3. **Observable by default.** Audit events on rebuild enable prompt-size monitoring without adding dedicated instrumentation.
4. **Extensible.** Third-party modules can follow the same contract by reading this ADR.

## When to Revisit

- **>10 subscribers**: Consider a base class or runtime validation.
- **Section key collision in production**: Add runtime duplicate-key detection.
- **Prompt size budget exceeded**: Add a prompt-size guard that rejects sections beyond a token budget.

## Consequences

### Positive
- Clear naming convention prevents section key collisions
- Consistent sanitization eliminates a class of prompt injection vectors
- Priority bands prevent unpredictable prompt ordering
- Audit events enable prompt churn monitoring

### Negative
- Convention requires discipline — no runtime enforcement
- New modules must read this ADR (low cost, high value)
- Priority band assignments may need rebalancing as modules grow
