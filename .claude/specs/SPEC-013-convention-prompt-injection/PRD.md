# PRD: Convention-Driven Prompt Injection

## Problem Statement

Agents don't know about their registered tools or teammates unless someone manually writes it into markdown files. This doesn't scale — every time a module registers new tools or a teammate joins, someone has to update identity.md or context.md. That's fragile and guaranteed to drift.

## Solution

Convention-driven injection: registries hold the metadata, and the system prompt is assembled from live registry state. Register a tool or entity and it automatically appears in the agent's system prompt. Zero manual maintenance.

## Requirements

### R1: Tool Catalog Injection

The agent's system prompt MUST automatically include a catalog of all registered tools with:
- Tool name and description (always present)
- When to use guidance (optional, from `when_to_use` field)
- Usage example (optional, from `example` field)
- Category grouping (optional, from `category` field)

**Acceptance criteria:**
- R1.1: Tools registered via `register()` appear in next prompt assembly
- R1.2: Tools removed via `shutdown()` disappear from next prompt assembly
- R1.3: Catalog uses XML format consistent with SkillRegistry
- R1.4: All string values are XML-escaped (defense in depth)
- R1.5: Catalog is cached and invalidated on `register()` calls
- R1.6: Empty catalog produces no section (no empty XML tags)

### R2: Team Roster Injection

When the messaging module is active, the agent's system prompt MUST include a roster of all registered entities with:
- All non-empty fields from the Entity model rendered dynamically
- No manual field enumeration — new Entity fields auto-appear

**Acceptance criteria:**
- R2.1: All entities from EntityRegistry appear in roster
- R2.2: Roster uses XML format consistent with tool catalog
- R2.3: All string values are XML-escaped
- R2.4: Roster refreshes on TTL-based schedule (default 60s)
- R2.5: Empty roster produces no section
- R2.6: New fields added to Entity model appear without code changes

### R3: RegisteredTool Metadata Fields

The `RegisteredTool` dataclass and `@native_tool` decorator MUST support:
- `when_to_use: str` — guidance on when the LLM should reach for this tool
- `example: str` — brief example of typical usage/arguments
- `category: str` — grouping label (e.g., "messaging", "filesystem")

**Acceptance criteria:**
- R3.1: Fields are optional with empty string defaults
- R3.2: `@native_tool` decorator accepts all three as keyword arguments
- R3.3: Fields flow through to `RegisteredTool` instances
- R3.4: Existing tools without these fields continue to work unchanged

### R4: Section Key Change

The messaging module's prompt section key MUST change from `sections['messaging']` to `sections['teams']`.

**Acceptance criteria:**
- R4.1: All team-related prompt content uses `sections['teams']`
- R4.2: No remaining references to `sections['messaging']`

### R5: Preamble Configuration

The tool catalog preamble text MUST be configurable via TOML.

**Acceptance criteria:**
- R5.1: Default preamble is hardcoded (works out of box)
- R5.2: Overridable via `[tools]` config section in TOML
- R5.3: Preamble text is XML-escaped

### R6: Roster TTL Configuration

The team roster refresh interval MUST be configurable.

**Acceptance criteria:**
- R6.1: `roster_ttl_seconds` field on MessagingConfig
- R6.2: Default value of 60 seconds
- R6.3: Configured via `[modules.messaging.config]` in TOML

### R7: Audit Events

Prompt catalog rebuilds MUST emit audit events (NIST 800-53 AU-2).

**Acceptance criteria:**
- R7.1: `prompt.tools_catalog_rebuilt` event on tool catalog rebuild
- R7.2: `prompt.roster_rebuilt` event on roster rebuild
- R7.3: Events include tool/entity count in details

## Out of Scope

- Tool Search / deferred loading (future, for 50+ tool catalogs)
- Semantic content injection defense beyond XML-escape (covered by module signing)
- Changes to EntityRegistry in arcteam (data source stays unchanged)
- Changes to how API tool schemas work (prompt catalog is complementary)

## Constraints

- Tool catalog works WITHOUT arcteam (single agent, no team)
- Team roster only activates when messaging module is enabled
- Token budget: catalog + roster should stay under ~500 tokens for typical setup
- Must not break existing identity.md / context.md flow — additive only
- Core LOC budget: <3,500 total (currently well within)
