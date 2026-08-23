<div align="center">

# 📄 arcokf

### **The Typed Document Contract for Arc Knowledge**
*Open Knowledge Format v0.2 — parse, validate, render, and index Markdown knowledge with structured diagnostics.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-19-0055BC.svg)](#status)
[![Coverage](https://img.shields.io/badge/coverage-91%25-003B82.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-0073FE.svg)](#status)
[![OKF](https://img.shields.io/badge/OKF-v0.2-F68D2E.svg)](#the-v02-document-shape)
[![Dependencies](https://img.shields.io/badge/deps-PyYAML_only-F68D2E.svg)](#install)

</div>

---

## ✨ What is arcokf?

`arcokf` is Arc's dependency-light **Open Knowledge Format (OKF) v0.2** contract. It parses
typed Markdown knowledge documents, rejects malformed YAML and unsafe links, and returns
structured diagnostics suitable for fail-closed stores.

A knowledge document is Markdown with YAML frontmatter carrying a **non-empty string `type`**.
That single required field is what makes a folder of Markdown a typed corpus rather than a pile
of notes: a store can refuse to write anything that would not parse back.

`arcokf` is deliberately **not** part of `arcmemory`. OKF is the canonical document boundary for
workspaces, shared knowledge, importers, and third-party plugins — those consumers must be able
to validate a document without installing a memory engine. Dependencies point from consumers such
as `arcmemory` *to* `arcokf`, never back upward, so either package can be removed or replaced
behind the same typed parse/render/validate contract without breaking unrelated function.

Its only runtime dependency is **PyYAML**. It imports no Arc package at all.

---

## ⭐ Top Features

What makes `arcokf` a safe document boundary rather than a Markdown reader:

### **Fail-Closed Validation**
- **Structured diagnostics, not exceptions** — `validate()` and `lint()` return a typed `ValidationResult` so a caller can report *every* problem before writing an invalid artifact
- **Typed diagnostic codes** — an 11-member `DiagnosticCode` enum (`missing_type`, `duplicate_key`, `wiki_link`, `reserved_document`, …) machine-routable by the calling store
- **Raise only where raising is right** — `parse()` and `render()` raise `OKFValidationError` carrying the same diagnostic tuple, for call sites that cannot proceed

### **Hardened YAML Frontmatter**
- **Strict loader** — a `SafeLoader` subclass: no arbitrary object construction, ever
- **Duplicate-key refusal** — a repeated frontmatter key is a `duplicate_key` diagnostic, not a silent last-write-wins overwrite
- **Alias-bomb ceiling** — more than 16 YAML aliases stops composition (billion-laughs class denial)
- **Bounded input** — 2 MB per document, 256 KB per frontmatter block, 32 levels of nesting

### **Link & Reserved-File Safety**
- **Wiki-links rejected** — `[[target]]` is not a standard OKF link; a body containing one fails validation rather than shipping a dangling reference
- **Reserved workspace files** — `context.md`, `index.md`, and `log.md` may omit frontmatter, and are refused if they try to *become* typed concept documents
- **Format boundary is explicit** — operational TOML, JSON, JSONL, signatures, and audit files are not OKF documents and are intentionally outside this contract

### **Deterministic Collection Indexes**
- **Canonical round-trip check** — an index that is not byte-identical to its own re-render is reported as non-canonical or tampered
- **Hash-verifiable inventory** — one SHA-256 digest over the whole canonical entry list, plus a per-document digest, so a stale or edited corpus is detectable without re-reading it
- **Classification floor** — only documents explicitly labelled `classification: unclassified` enter a shared index; missing, malformed, or elevated labels are never listed
- **Path guards** — absolute paths, `..` traversal, non-`.md` suffixes, reserved names, and operational directories (`.git`, `.arc`, `audit`, `secrets`, `credentials`, …) are refused at entry construction

---

## 🏗️ Where It Fits

```text
arcagent · arccli · arcmemory · arcteam      consumers — parse/render/validate
        |
        v
     arcokf                                  the typed document contract
        |
        v
     PyYAML                                  the only runtime dependency
```

`arcokf` is a **true leaf** — it imports nothing from Arc, not even `arctrust`. Dependencies point
one way only: a consumer imports `arcokf`, and `arcokf` never imports a consumer. That is what lets
an importer or a third-party plugin validate a knowledge document with no agent, memory engine, or
crypto stack installed.

---

## 🚀 Install

```bash
pip install arcokf           # standalone — validate documents with no Arc stack
# or
pip install arcmas           # full Arc stack (arcokf arrives with arcmemory)
```

---

## 🧪 Quick Example

```python
from pathlib import Path

from arcokf import Document, lint, parse, render, validate

# 1. Author a typed document. `type` is the one required frontmatter field.
document = Document({"type": "Entity", "title": "Arc"}, "# Arc\n")

# 2. Render it — frontmatter is emitted with sorted keys, and the result is
#    validated before it is returned, so render() never hands back a bad artifact.
encoded = render(document)
assert validate(encoded).valid

# 3. Parse it back. parse() raises OKFValidationError on invalid input.
parsed = parse(encoded)
assert parsed.metadata["type"] == "Entity"

# 4. Lint a file on disk. lint() never raises — it returns every diagnostic
#    so a fail-closed store can report all of them and write nothing.
result = lint(Path("workspace/knowledge/arc.md"))
if not result.valid:
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message, diagnostic.path)
```

---

## 📐 The v0.2 Document Shape

A knowledge document is YAML frontmatter with a non-empty string `type`, followed by a Markdown
body:

```markdown
---
type: Entity
title: Arc
tags: [agent, platform]
---
# Arc

Arc is an accountable agent framework.
```

Reserved workspace files — `context.md`, `index.md`, `log.md` — may omit frontmatter. Operational
TOML, JSON, JSONL, signature, and audit files are not OKF documents.

| Limit | Value | Why |
|---|---|---|
| `MAX_DOCUMENT_BYTES` | 2,000,000 | A knowledge document is a document, not a data dump |
| `MAX_FRONTMATTER_BYTES` | 256,000 | Metadata is metadata; the body carries the content |
| `MAX_NESTING` | 32 | Bounds recursive structure walks |
| `MAX_ALIASES` | 16 | Stops alias-expansion (billion-laughs) denial of service |

---

## 🧩 What's Inside

### Documents & Validation (`arcokf.core`)

| Symbol | What It Does |
|---|---|
| `Document` | Frozen dataclass: `metadata` mapping, Markdown `body`, optional source `path` |
| `validate(source, *, path=None)` | Validate `str` or `bytes`. Never raises — returns a `ValidationResult` |
| `parse(source, *, path=None)` | Validate then return the `Document`; raises `OKFValidationError` if invalid |
| `render(document)` | Serialize to `---` frontmatter (sorted keys, unicode preserved) + body, validating the result before returning it |
| `lint(path)` | Validate one UTF-8 file from disk, retaining its POSIX path on every diagnostic. An unreadable file is a diagnostic, not an exception |
| `ValidationResult` | `valid`, `diagnostics` tuple, and the `Document` when — and only when — it is clean |
| `Diagnostic` | Frozen `code` / `message` / `path` triple |
| `DiagnosticCode` | The `StrEnum` of every refusal reason |
| `OKFValidationError` | Raised by `parse` / `render`; carries the full `diagnostics` tuple |
| `VERSION` | `"0.2"` — the contract version this build implements |

**The diagnostic codes:** `invalid_utf8`, `invalid_frontmatter`, `duplicate_key`, `missing_type`,
`invalid_type`, `document_too_large`, `frontmatter_too_large`, `nesting_too_deep`,
`too_many_aliases`, `wiki_link`, `reserved_document`.

**Why diagnostics instead of exceptions:** a store that is about to write a batch of documents needs
to know *everything* wrong with them before it writes any of them. An exception reports the first
problem and destroys the rest of the pass; a `ValidationResult` reports all of them and leaves the
decision — and the write — to the caller.

### Collection Indexes (`arcokf.index`)

A collection index is deliberately a plain reserved `index.md`. Machine-readable HTML entry comments
make validation unambiguous while the adjacent Markdown links stay useful to a person reading the
folder. `arcokf` only renders and validates; the owning store decides when to write one.

| Symbol | What It Does |
|---|---|
| `CollectionEntry` | One authorized document: relative `path`, `title`, SHA-256 `digest`, one-line `summary` |
| `render_collection_index(entries)` | Render the canonical index — sorted by path, with a whole-inventory SHA-256 and a per-entry JSON comment |
| `validate_collection_index(index, root=None)` | Parse and canonicality-check an index; with a `root`, also re-inventory the tree, re-digest every listed file, and re-lint it as OKF |
| `inventory_documents(root)` | Deterministically enumerate the authorized documents under `root` |
| `document_entry(path, root)` | Build one entry for a single document without walking its siblings; returns `None` when the document is invalid or not `unclassified` |
| `CollectionIndexValidation` | `valid`, `error`, and the parsed `entries` |
| `CollectionIndexError` | Raised internally for any entry that cannot be represented safely; surfaced as `error` text |

Short aliases `render_index` and `validate_index` live on the submodule (`arcokf.index`); the
explicit names are the documented public API off the package root.

**Why the round-trip check:** the index re-renders the entries it just parsed and compares the result
to the file byte for byte. Anything a hand edit could do — reordering, an added link line, a tweaked
summary, a swapped digest — changes those bytes, so tamper detection needs no separate signature and
no second source of truth.

**Why the classification floor:** a shared index is safe for the lowest clearance only. A document
with a missing, malformed, or elevated `classification` label is silently omitted rather than listed,
so an index can never advertise the existence of something its readers may not see. A
higher-clearance collection gets its own owner and its own index.

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arcokf/tests
```

- **Tests:** 19
- **Coverage:** 91%
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
