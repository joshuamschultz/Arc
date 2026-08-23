# arcokf

`arcokf` is Arc's dependency-light Open Knowledge Format (OKF) v0.2 contract.
It parses typed Markdown knowledge documents, rejects malformed YAML and unsafe
links, and returns structured diagnostics suitable for fail-closed stores.

It is intentionally not part of `arcmemory`. OKF is the canonical document
boundary for workspaces, shared knowledge, importers and third-party plugins;
those consumers must be able to validate documents without installing a memory
engine. Dependencies point from consumers such as arcmemory to arcokf, never
back upward. Either package can therefore be removed or replaced behind the
same typed parse/render/validate contract without breaking unrelated function.

## v0.2 document shape

Knowledge documents use YAML frontmatter with a non-empty string `type` and a
Markdown body:

```markdown
---
type: Entity
title: Arc
tags: [agent, platform]
---
# Arc

Arc is an accountable agent framework.
```

Reserved workspace files such as `context.md`, `index.md`, and `log.md` may
omit frontmatter. Operational TOML, JSON, JSONL, signatures, and audit files
are not OKF documents and are intentionally outside this contract.

## Python API and linter

```python
from pathlib import Path
from arcokf import Document, lint, parse, render, validate

document = Document({"type": "Entity", "title": "Arc"}, "# Arc\n")
encoded = render(document)
assert validate(encoded).valid
parsed = parse(encoded)
result = lint(Path("workspace/knowledge/arc.md"))
if not result.valid:
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message, diagnostic.path)
```

`parse()` raises `OKFValidationError` on invalid input. `validate()` and
`lint()` return typed `ValidationResult` values so callers can report all
diagnostics without writing an invalid artifact.
