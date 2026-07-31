# arcprompt - Prompt Engine

> **Building with Arc**  ·  Build  ·  page 14 of 27  
> **For** Engineers writing code against Arc  
> [← arcstore](arcstore.md)  ·  [Docs home](../../README.md)  ·  [arcmodel →](arcmodel.md)

---

## Overview

`arcprompt` provides **prompt building and management**:
- **Prompt templates** - Reusable prompt structures
- **Context injection** - Skills, memory, history
- **Dynamic building** - Runtime prompt construction
- **Template catalog** - Pre-built templates

```mermaid
flowchart LR
    classDef prompt fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef template fill:#002550,stroke:#001A38,color:#FFFFFF

    Task[Task]:::prompt --> arcprompt[arcprompt<br/>Prompt Engine]:::prompt
    arcprompt --> System[System Prompt]:::template
    arcprompt --> Context[Context<br/>Memory + Skills]:::template
    arcprompt --> History[History<br/>Session]:::template
    System --> Messages[Messages<br/>for LLM]:::prompt
    Context --> Messages
    History --> Messages
```

---

## Prompt Building

### Basic Builder

```python
from arcprompt import render_prompt

messages = render_prompt(
    task="Analyze the sales data",
    system_prompt="You are a helpful analyst.",
    context=[
        {"role": "user", "content": "Previous query"},
        {"role": "assistant", "content": "Previous response"}
    ],
    skills=["data-analysis", "chart-generation"]
)

# Returns list of message dicts
# [
#   {"role": "system", "content": "..."},
#   {"role": "user", "content": "..."},
#   ...
# ]
```

### Template System

```python
from arcprompt import PromptDocument

template = PromptDocument.from_file("templates/analyst.md")

messages = template.render(
    task="Analyze Q4 data",
    data_files=["sales.csv", "customers.csv"],
    skills=["data-analysis"]
)
```

---

## Prompt Templates

### Default Templates

| Template | Purpose |
|----------|---------|
| `default` | General assistant |
| `researcher` | Research-oriented |
| `coder` | Code-focused |
| `analyst` | Data analysis |
| `assistant` | Chat assistant |

### Custom Templates

```markdown
---
name: custom-analyst
version: 1.0.0
variables: [task, data_context, skills]
---

# Custom Analyst

{{ system_prompt }}

## Task
{{ task }}

## Data Context
{% for file in data_context %}
- {{ file.name }}: {{ file.description }}
{% endfor %}

## Available Skills
{% for skill in skills %}
- {{ skill.name }}: {{ skill.description }}
{% endfor %}

## Instructions
Follow these steps:
1. Load the data
2. Analyze for trends
3. Report findings
```

---

## Context Injection

### Skill Context

```python
def inject_skill_context(skills: list[Skill]) -> str:
    """Inject skill instructions into prompt."""
    context = ""
    for skill in skills:
        context += f"\n## {skill.name}\n{skill.frontmatter.steps}"
    return context
```

### Memory Context

```python
def inject_memory_context(memory_hits: list[MemoryHit]) -> str:
    """Inject relevant memories into prompt."""
    context = "\n## Relevant Context\n"
    for hit in memory_hits[:5]:  # Top 5
        context += f"- {hit.content}\n"
    return context
```

---

## API Reference

### Functions

```python
def render_prompt(
    task: str,
    system_prompt: str = None,
    context: list[dict] = None,
    skills: list[Skill] = None,
    memory: list[MemoryHit] = None
) -> list[dict]:
    """Build prompt messages for LLM."""

def inject_context(
    prompt: list[dict],
    context_type: str,
    content: str
) -> list[dict]:
    """Inject context into prompt."""

def truncate_prompt(
    prompt: list[dict],
    max_tokens: int,
    model: str
) -> list[dict]:
    """Truncate prompt to fit context window."""
```

### Classes

```python
class PromptDocument:
    def __init__(self, template: str): ...
    
    @classmethod
    def from_file(cls, path: Path) -> PromptDocument: ...
    
    def render(self, **variables) -> str: ...
    
    def to_messages(self, content: str) -> list[dict]: ...

class PromptBuilder:
    def __init__(self, config: dict): ...
    
    def add_system(self, content: str) -> PromptBuilder: ...
    def add_context(self, content: str) -> PromptBuilder: ...
    def add_history(self, messages: list[dict]) -> PromptBuilder: ...
    def build(self) -> list[dict]: ...
```

---

## Token Management

### Counting

```python
def count_tokens(text: str, model: str) -> int:
    """Count tokens in text."""
    # Uses provider-specific tokenizer
    ...

def count_prompt_tokens(prompt: list[dict], model: str) -> int:
    """Count tokens in prompt."""
    ...
```

### Truncation

```python
def truncate_to_fit(
    prompt: list[dict],
    max_tokens: int,
    model: str
) -> list[dict]:
    """Truncate prompt to fit context window."""
    
    # Remove oldest context first
    # Then truncate oldest messages
    # Never remove system prompt
    ...
```

---

## Next Steps

- [API Reference](../../reference/api.md) - Complete API documentation
- [Package Index](../package-index.md) - All Arc packages

---

## Verified public surface

> Introspected from the installed package on the current commit. Every name
> below is importable exactly as shown; full signatures are in the
> [API reference](../../reference/api.md#arcprompt).

### Classes

| Class | Purpose |
|---|---|
| `PromptCatalog` | Enumerate stock prompts across a fixed set of installed packages. |
| `PromptDocument` | A resolved prompt: its validated frontmatter, body, and derived identity. |
| `PromptError` | Base class for every arcprompt failure. |
| `PromptFrontmatter` | Validated frontmatter carried by every prompt file. |
| `PromptMissing` | A packaged stock prompt is absent at load — a packaging error. |
| `PromptRef` | A discovered stock prompt: enough to list and locate it without loading overlays. |
| `PromptResolver` | Resolve a prompt to its effective body, overlay-over-stock, first-match-wins. |
| `PromptSnapshot` | Immutable per-run mapping of ``(package, name)`` to its resolved document. |
| `PromptUnparseable` | An overlay or stock file has malformed frontmatter or an empty body. |
| `PromptUnsigned` | An overlay's Ed25519 signature is missing, invalid, or wrong-key. |
| `SignatureVerifier` | Verify an overlay's detached signature against a single pinned public key. |
| `TrustPosture` | Deployment stringency carried for provenance/audit context. |

### Functions

| Function | Signature |
|---|---|
| `load_stock` | `(package: 'str', name: 'str') -> 'str'` |
| `load_stock_document` | `(package: 'str', name: 'str') -> 'PromptDocument'` |
| `parse_prompt` | `(raw: 'bytes', *, source: 'Source', signer_did: 'str \| None' = None) -> 'PromptDocument'` |
| `render_prompt` | `(body: 'str', *, name: 'str', description: 'str', tunable: 'bool' = True) -> 'bytes'` |
| `snapshot` | `(resolver: 'PromptResolver', refs: 'Sequence[PromptRef]', *, actor_did: 'str', sink: 'AuditSink', re` |

