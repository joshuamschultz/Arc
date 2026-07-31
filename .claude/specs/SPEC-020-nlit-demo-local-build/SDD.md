# SPEC-020 — SDD: NLIT 2026 Demo (Local Arc Agent Build)

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                       DEMO MACHINE (laptop, macOS)                   │
│                                                                      │
│  ┌──────────────────────────┐    ┌──────────────────────────┐        │
│  │  team/nlit_soc_agent/    │    │  team/nlit_cora_agent/   │        │
│  │  • served (long-lived)   │    │  • served (long-lived)   │        │
│  │  • conversational        │    │  • [modules.scheduler]   │        │
│  │  • ANTHROPIC_API_KEY     │    │  • cron 0 4 * * * UTC    │        │
│  │                          │    │                          │        │
│  │  workspace/              │    │  workspace/              │        │
│  │   ├ identity.md          │    │   ├ identity.md          │        │
│  │   ├ templates/ (9)       │    │   ├ Reports/             │        │
│  │   └ entities/ ←──────────────────────── Obsidian (left  │        │
│  │     ├ System/            │    │   pane: graph view)     │        │
│  │     ├ Event/             │    │                          │        │
│  │     ├ STIG-Reference/    │    │  demo-data/              │        │
│  │     ├ Incident/          │    │   ├ system_inventory.md  │        │
│  │     ├ Finding/           │    │   ├ stig_checklist.csv   │        │
│  │     ├ Person/            │    │   ├ poam_log.csv         │        │
│  │     ├ Vendor/            │    │   └ org_chart.json       │        │
│  │     ├ Process/           │    │                          │        │
│  │     └ Project/           │    │  tools/                  │        │
│  │  tools/                  │    │   ├ read_document.py     │        │
│  │   └ write_entity.py      │    │   ├ stig_cross_ref.py    │        │
│  │                          │    │   ├ poam_validator.py    │        │
│  │                          │    │   └ draft_gap_report.py  │        │
│  └─────────┬────────────────┘    └─────────┬────────────────┘        │
│            │                                │                        │
│            │ websocket: ui_reporter         │                        │
│            ▼                                ▼                        │
│  ┌──────────────────────────────────────────────────────┐            │
│  │  arcui dashboard @ localhost:8420 (right pane)       │            │
│  │  • LLM telemetry (existing)                          │            │
│  │  • Tool call timeline (existing)                     │            │
│  │  • + Schedule History card (NEW — this spec)         │            │
│  └──────────────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────────────┘
            │ (only external dep)
            ▼
       Anthropic API (Claude Sonnet 4.5/4.6)
       — dedicated demo API key —
```

## 2. Module Boundaries

| Boundary | What lives here | What does NOT live here |
|---|---|---|
| `team/nlit_soc_agent/` | Agent 1 config, identity, templates, write_entity skill, anchor prompts | No CORA logic, no shared imports, no shared vault |
| `team/nlit_cora_agent/` | Agent 2 config, identity, 4 CORA skills, demo data, report templates | No SOC logic, no shared imports, no shared vault |
| `packages/arcui/` (extended) | + Schedule History card (HTML/JS partial + small backend handler) | Not the schedule logic itself (that lives in arcagent); not agent-specific code |
| `packages/arcagent/`, `arcrun/`, `arcllm/` | Unmodified | Anything in this spec |

**Pillar 2 invariant:** No file in this spec imports from another agent's directory or from `team/shared/`.

## 3. Agent 1 — `nlit_soc_agent`

### 3.1 Configuration (`arcagent.toml`)

Copy the structure from `team/my_agent/arcagent.toml`. Diff:

```toml
[agent]
name = "nlit_soc_agent"
org = "ctg-federal-demo"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-sonnet-4-6"   # Sonnet honors temperature; Opus 4.7 doesn't
max_tokens = 4096
temperature = 0                          # Decision 7 update — temp=0
# DO NOT enable extended thinking — extended thinking forces temp=1

[identity]
did = ""                                 # auto-generated on first run
key_dir = "~/.arcagent/keys"
vault_path = ""                          # personal tier: file-based keys

[extensions]
paths = []                               # Decision 3: no shared skill paths
workspace_tools_dir = "tools"

[modules.ui_reporter]
enabled = true

[modules.ui_reporter.config]
url = "ws://localhost:8420/api/agent/connect"
token = ""                               # resolves from ~/.arcagent/ui-token

[modules.scheduler]
enabled = false                          # Agent 1 is conversational, no schedule

[modules.bio_memory]
enabled = false                          # Decision 4: no memory infra for v1

[modules.policy]
enabled = true                           # default policies; demo doesn't need custom

[telemetry]
enabled = true
service_name = "nlit_soc_agent"
log_level = "INFO"

[context]
max_tokens = 200000
prune_threshold = 0.70
compact_threshold = 0.85
emergency_threshold = 0.95
```

### 3.2 Identity (`workspace/identity.md`)

Plain markdown (NOT YAML — confirmed by `/deepen`). Sections:

- **Name**: SOC Threat Hunter
- **Role**: SOC analyst at a federal IT shop. STIG/security focus.
- **Authority**: Read · Triage · Capture entities · Correlate · Draft incident notes. NO containment actions.
- **Core Truths** — what the agent believes about its job
- **Tool Triggers** (the highest-leverage section per Anthropic guidance):
  > Whenever the user mentions a specific IT or security entity by name — host, server, workstation, subnet, service, user, CVE, network device, STIG ID, finding, incident — call `write_entity` immediately. Extract entity type and all properties mentioned. Scan the conversation history for previously-mentioned entities and add `[[wikilinks]]` to their names. **Do not explain what you are doing or ask for confirmation — just call this tool immediately.**
- **Decision Framework** — how to pick entity type (host → System; SIEM event → Event; STIG → STIG-Reference; etc.)
- **Communication Style** — terse, no preamble, one-sentence acknowledgment after tool call
- **Lessons Learned** — STIG-specific gotchas (CCRI was renamed CORA in 2024-03; "finding" ≠ "vulnerability" ≠ "weakness"; CCI is many-to-many with NIST 800-53; severity in XCCDF XML is `high|medium|low` but UI displays CAT I/II/III)
- **Boundaries** — what the agent will NOT do
- **About the User** — the presenter chatting with it on stage

### 3.3 Few-shot Examples (loaded as message-format pre-conversation turns)

Per LangChain benchmark (Sonnet 16%→52% reliability with 3 message-format examples). Stored in `workspace/few-shots.json`:

```json
[
  {
    "role": "user",
    "content": "Just met with Jane Doe about the RHEL upgrade on host-alpha. She's the system owner."
  },
  {
    "role": "assistant",
    "content": [
      {"type": "tool_use", "id": "fs1", "name": "write_entity", "input": {
        "entity_type": "System", "name": "host-alpha",
        "properties": {"primary_os": "RHEL 9", "system_owner": "[[Jane Doe]]"},
        "wikilinks": ["[[Jane Doe]]"]
      }},
      {"type": "tool_use", "id": "fs1b", "name": "write_entity", "input": {
        "entity_type": "Person", "name": "Jane Doe",
        "properties": {"role": "ISSO", "owns_systems": ["[[host-alpha]]"]},
        "wikilinks": ["[[host-alpha]]"]
      }}
    ]
  },
  {
    "role": "tool",
    "content": [{"type": "tool_result", "tool_use_id": "fs1", "content": "Wrote workspace/entities/System/host-alpha.md"},
                {"type": "tool_result", "tool_use_id": "fs1b", "content": "Wrote workspace/entities/Person/Jane Doe.md"}]
  },
  // ... 2 more example pairs
]
```

Loaded by a thin wrapper at agent-start that prepends them to the conversation. Mechanism TBD in `/implement`: either (a) an arcagent context-init hook, (b) injected as `system` continuation, or (c) loaded by a small `bootstrap.py` extension. Prefer (c) — explicit and isolated.

### 3.4 `write_entity` skill (`workspace/tools/write_entity.py`)

Pattern: copy `packages/arcagent/src/arcagent/tools/write.py` factory. Key shape:

```python
"""write_entity — template-driven entity writer for the SOC threat hunter."""
from pathlib import Path
from string import Template
from typing import Any

from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.tools._validation import resolve_workspace_path

_VALID_TYPES = {
    "System", "Event", "STIG-Reference", "Incident", "Finding",
    "Person", "Vendor", "Process", "Project",
}

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "entity_type": {"type": "string", "enum": sorted(_VALID_TYPES)},
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "properties": {"type": "object", "additionalProperties": True},
        "wikilinks": {"type": "array", "items": {"type": "string"}, "default": []},
        "body": {"type": "string", "default": ""},
    },
    "required": ["entity_type", "name", "properties"],
    "additionalProperties": False,
}

def create_tool(workspace: Path, *, allowed_paths: list[Path] | None = None) -> RegisteredTool:
    ws = workspace.resolve()
    templates_dir = ws / "templates"

    async def execute(*, entity_type: str, name: str, properties: dict[str, Any],
                      wikilinks: list[str] | None = None, body: str = "",
                      **_: Any) -> str:
        if entity_type not in _VALID_TYPES:
            return f"Error: unknown entity_type '{entity_type}'. Must be one of {sorted(_VALID_TYPES)}."

        template_path = templates_dir / f"{entity_type}.md"
        if not template_path.exists():
            return f"Error: template missing at {template_path}"

        content = _render(template_path, name, properties, wikilinks or [], body)

        # Sanitize filename (per /deepen Obsidian gotcha — no special chars)
        safe_name = _safe_filename(name)
        rel_path = f"entities/{entity_type}/{safe_name}.md"
        out = resolve_workspace_path(rel_path, ws, allowed_paths=allowed_paths)
        out.parent.mkdir(parents=True, exist_ok=True)

        # Atomic rewrite (avoids Obsidian vault cache truncation bug)
        tmp = out.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(out)

        return f"Wrote {rel_path}"

    return RegisteredTool(
        name="write_entity",
        description=(
            "Call this tool whenever the user mentions a specific IT or security entity by name "
            "(host, server, subnet, service, user, CVE, network device, STIG ID, finding, incident). "
            "Extract entity_type and all properties mentioned. Scan the conversation history for "
            "previously-mentioned entities and add [[wikilinks]] to their names. "
            "Do not explain what you are doing or ask for confirmation — call this tool immediately."
        ),
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="nlit_soc_agent.write_entity",
        classification="state_modifying",
        capability_tags=["entity_write", "workspace_write"],
    )
```

`_render()` loads the template, fills frontmatter from `properties` (with proper YAML quoting per `/deepen` gotchas — colons, dates, wikilinks all quoted), appends body + wikilinks-rendered relationship section. Plain Python string handling — no Jinja.

### 3.5 Entity Templates (`workspace/templates/{Type}.md`)

9 templates, all using **real federal field names** verified during `/deepen`. Example shape for `STIG-Reference.md`:

```markdown
---
type: stig-reference
vuln_id: $vuln_id            # V-XXXXXX
rule_id: $rule_id            # SV-XXXXXXrXXXXXXX_rule
stig_id: $stig_id            # e.g., RHEL-09-211010
severity: $severity          # high | medium | low (XCCDF)
severity_cat: $severity_cat  # CAT I | CAT II | CAT III (display)
weight: $weight
cci: $cci_list               # list of CCI-XXXXXX
control_family: $control_family   # AC|AT|AU|CA|CM|CP|IA|IR|MA|MP|PE|PL|PM|PS|PT|RA|SA|SC|SI|SR
title: "$title"              # always quoted (colons in titles)
status: $status              # Open | Not a Finding | Not Applicable | Not Reviewed
created: $created
source_agent: $source_agent
---

## Vuln Discussion
$vuln_discussion

## Check Content
$check_content

## Fix Text
$fix_text

## Related
$wikilinks_section
```

Other templates (`System.md`, `Event.md`, `Incident.md`, `Finding.md`, `Person.md`, `Vendor.md`, `Process.md`, `Project.md`) follow the same shape with type-appropriate fields. Field lists per `/deepen` research insights §5.

### 3.6 Anchor prompts (`workspace/demo-prompts/stage-anchors.md`)

Plain markdown. 5 anchor phrasings (PRD §7). Presenter reads from this; agent never loads it.

## 4. Agent 2 — `nlit_cora_agent`

### 4.1 Configuration (`arcagent.toml`)

```toml
[agent]
name = "nlit_cora_agent"
org = "ctg-federal-demo"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-sonnet-4-6"
max_tokens = 8192
temperature = 0

[identity]
did = ""
key_dir = "~/.arcagent/keys"

[extensions]
paths = []
workspace_tools_dir = "tools"

[modules.ui_reporter]
enabled = true

[modules.ui_reporter.config]
url = "ws://localhost:8420/api/agent/connect"
token = ""

[modules.scheduler]
enabled = true

[modules.scheduler.config]
min_interval_seconds = 60
max_schedules = 50
default_timeout_seconds = 600        # 10 min for compliance pipeline
max_timeout_seconds = 3600
circuit_breaker_threshold = 3
check_interval_seconds = 30

[telemetry]
enabled = true
service_name = "nlit_cora_agent"
```

Schedule registered at first run via `schedule_create` tool (cron `0 4 * * *` UTC). Persisted in `~/.arcagent/schedules.json` (per scheduler module's `ScheduleStore`).

### 4.2 Schedule prompt

The scheduler fires a natural-language prompt (per `/deepen` finding — schedules don't fire tool calls directly). Prompt registered:

> "Run the CORA compliance audit. Read system_inventory.md, stig_checklist.csv, poam_log.csv, and org_chart.json from `./demo-data/`. Cross-reference STIGs against systems. Validate POA&M closures against system event logs. Surface any false closures. Identify findings with missing or stale owners. Draft a gap report and write it to `./workspace/Reports/CORA-Gap-Report-{date}.md`."

### 4.3 Identity (`workspace/identity.md`)

Same structure as Agent 1, different content. Key sections:

- **Role**: Senior Compliance Analyst, CORA & FISMA programs
- **Tool Triggers**: Use `read_document` first, then `stig_cross_reference`, then `poam_validator`, then `draft_gap_report`. Always in this order for a compliance audit.
- **Lessons Learned**: Closure dates before system reimaging = false closure (planted V-220812 example documented). POA&M owners go stale; verify against current org chart. STIG V-220718 has known false-positive on RHEL 9.3 — document, don't flag. Severity nomenclature (high|medium|low XCCDF = CAT I|II|III display).

### 4.4 Skills

| Skill | Pattern | Inputs | Output |
|---|---|---|---|
| `read_document.py` | Same factory as `write_entity` | `file_path` | Parsed structure (CSV → list[dict], MD → text, JSON → dict) |
| `stig_cross_reference.py` | Pure logic, no LLM | `stig_checklist`, `system_inventory` | List of findings: `{stig_id, system_id, severity, status, owner}` |
| `poam_validator.py` | Pure logic, no LLM — **deterministic** | `poam_entries`, `system_inventory.event_log` | `{validated_poam, false_closures}` — date comparison surfaces V-220812 |
| `draft_gap_report.py` | LLM call (Claude) to format the report markdown | `findings`, `false_closures`, `unowned`, `past_due` | Markdown string |

Note: `poam_validator` is **deterministic Python**, not LLM-driven. The false-closure surface is testable with assertions — does not depend on Claude's reasoning.

### 4.5 Demo data (`demo-data/`)

| File | Format | Notes |
|---|---|---|
| `system_inventory.md` | Markdown with YAML frontmatter blocks | 4 systems. Real federal fields: `system_name`, `system_id`, `fips_199_categorization`, `system_owner`, `isso`, `issm`, `ato_date`, `primary_os`, `ip_range`, `criticality`. host-alpha (System-A) has `reimaging_date: 2026-03-17`. |
| `stig_checklist.csv` | CSV | 40 rows. Fields: `vuln_id, rule_id, stig_id, severity, severity_cat, system_id, status, closure_date`. **V-220812 row**: `vuln_id=V-220812, severity=high, severity_cat=CAT I, system_id=host-alpha, status=Closed, closure_date=2026-03-03`. |
| `poam_log.csv` | CSV | 25 rows. Real POA&M fields: `poam_id, weakness_description, weakness_detector_source, severity, scheduled_completion_date, status, point_of_contact, corrective_action_plan`. **POA-2026-003 row** references V-220812. |
| `org_chart.json` | JSON | 8 staff records. host-beta has no current ISSO (stale owner planted). |

## 5. arcui Schedule History Card

### 5.1 Boundary

Lives in `packages/arcui/src/arcui/static/`. ONE new HTML partial + small JS. Subscribes to existing event types `schedule:completed` and `schedule:failed` flowing through `[modules.ui_reporter]`.

### 5.2 Files

- `packages/arcui/src/arcui/static/components/schedule-history.html` — small card markup
- `packages/arcui/src/arcui/static/js/schedule-history.js` — event subscription + render
- (If verification in PRD §9 Q2 confirms ui_reporter doesn't currently forward `schedule:*` events:) update `packages/arcagent/src/arcagent/modules/ui_reporter/__init__.py` to subscribe to scheduler bus events. ~10 lines.

### 5.3 Render

```
┌─────────────────────────────────┐
│ ⏰ Schedule History              │
├─────────────────────────────────┤
│ Last fired:  2026-04-27 04:00 UTC│
│ Status:      ✓ ok (5.2s)        │
│ Next fires:  2026-04-28 04:00 UTC│
├─────────────────────────────────┤
│ Recent runs:                    │
│ • 2026-04-27 04:00 ✓ 5.2s       │
│ • 2026-04-26 04:00 ✓ 4.8s       │
│ • 2026-04-25 04:00 ✓ 5.1s       │
│ • 2026-04-24 04:00 ✓ 5.3s       │
│ • 2026-04-23 04:00 ✓ 4.9s       │
└─────────────────────────────────┘
```

Bounded to last 5 runs in DOM (memory-safe).

## 6. Stage Setup

| Component | Setup |
|---|---|
| Obsidian | Open `team/nlit_soc_agent/workspace/` as a vault. Install **Refresh Any View** plugin. Configure graph color groups: System=blue, Event=red, STIG-Reference=yellow, Incident=orange, Finding=purple, Person=green, Vendor=cyan, Process=gray, Project=pink. |
| arcui | Started before agent. `arc ui start` (per SPEC-019 zero-config flow). Token at `~/.arcagent/ui-token`. |
| Both agents | Started in two separate terminals. `cd team/nlit_soc_agent && arcagent serve` and `cd team/nlit_cora_agent && arcagent serve`. |
| Schedule | Registered via `nlit_cora_agent` chat once: "Create a schedule, type=cron, expression='0 4 * * *', prompt='Run the CORA compliance audit...'". Persists to `~/.arcagent/schedules.json`. |
| API key | `export ANTHROPIC_API_KEY=...` from a **dedicated demo key** (not production). |
| Network | Wired ethernet preferred; tethered hotspot as backup (per Meta Connect 2025 lesson). |

## 7. Data Flow

### 7.1 Agent 1 conversation turn

```
Presenter typed prompt
  ↓
arcagent receives
  ↓
arcllm — call Claude (temp=0, with 3 few-shots prepended, with write_entity in tools)
  ↓
Claude returns tool_use(write_entity)
  ↓
arcagent dispatches to write_entity skill
  ↓
write_entity loads template, renders frontmatter+body+wikilinks, atomic-writes file
  ↓
Returns "Wrote entities/System/host-alpha.md"
  ↓
Obsidian file watcher detects, Refresh Any View triggers graph refresh
  ↓
arcui receives agent:post_tool event with details
  ↓
Audience sees: file appears in Obsidian + tool call appears in arcui timeline
```

### 7.2 Agent 2 scheduled fire

```
[modules.scheduler] timer loop (check every 30s)
  ↓
Cron evaluates: 0 4 * * * UTC matches → ScheduleEntry dequeued
  ↓
agent_run_fn(prompt, tool_choice={"type": "any"})
  ↓
arcllm calls Claude with prompt + 4 CORA tools available
  ↓
Claude orchestrates: read_document → stig_cross_reference → poam_validator → draft_gap_report
  ↓
draft_gap_report writes Reports/CORA-Gap-Report-{date}.md
  ↓
Schedule completes; emits schedule:completed bus event
  ↓
ui_reporter forwards to arcui
  ↓
arcui Schedule History card updates: "Last fired 04:00 UTC, ok, 5.2s"
  ↓
Audience walks in next morning: report file is there, history card shows the run
```

## 8. Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| Claude misses a write_entity call on stage | Recovery line: "Log that — host-alpha, subnet 10.0.1, type System." Imperative reformulation reliably re-triggers per /deepen. |
| Obsidian doesn't refresh graph | Refresh Any View plugin pre-installed. Manual click-refresh as backup. |
| arcui websocket disconnects | 1000-event local buffer + auto-reconnect with decorrelated jitter (existing arcui behavior). |
| Schedule doesn't fire at 04:00 UTC | Verify timezone in rehearsal. Scheduler timer loop runs every 30s; max delay = 60s after cron match. |
| Anthropic API outage | No mitigation in v1 (no Nemotron fallback per scope decision). Demo runbook notes: have laptop on tethered hotspot as backup network. |
| `poam_validator` false-positive on a non-planted row | Logic is deterministic date comparison — testable with assertions in unit tests. Pre-flight test asserts only V-220812 surfaces. |
| Vault cache truncation on file write | Atomic write (temp + rename), never edit-in-place — already in skill design. |
| Claude over-explains / preambles | `tool_choice={"type": "tool", "name": "write_entity"}` for known entity-mention turns + tool description has explicit pacing instruction. |

## 9. Test Strategy

### 9.1 Unit tests (per skill)

- `write_entity`: 9 tests (one per entity type), property serialization (colons quoted, dates as strings, wikilinks quoted), atomic write behavior, path traversal rejection, missing-template error.
- `read_document`: CSV / MD / JSON parsing, missing file, malformed input.
- `stig_cross_reference`: known input → known finding output (table-driven).
- `poam_validator`: **assertion** — given the planted demo data, exactly one false closure (V-220812) is reported. Other 24 POA&M rows pass.
- `draft_gap_report`: snapshot test of report structure (sections, headers).

### 9.2 Integration tests

- Agent 1 end-to-end: load identity + few-shots, send 6 anchor prompts, assert ≥5 result in `write_entity` tool calls with correct types. Run against real Anthropic API (sonnet-4-6, temp=0). Tracked in CI as a slow test.
- Agent 2 end-to-end: load CORA pipeline prompt, run end-to-end, assert report file exists + contains "V-220812" + contains "false closure".

### 9.3 Rehearsal (manual, pre-stage)

5-rehearsal pre-flight per `/deepen` research §6:
- T-7d Baseline: 6/6 anchors fire.
- T-5d Variation: rephrased anchors still fire.
- T-3d Recovery: deliberate ambiguity, practice recovery line.
- T-1d Environment: actual demo machine, actual venue network.
- AM-of Dress: full run-through with someone watching.

## 10. Decisions Traceability

Every requirement in PRD maps to a decision in `.claude/decisions-log.md` § "Build Decisions: nlit-demo-local-build" + § "Resolutions from /deepen". README.md substrate table is the index.
