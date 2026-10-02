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

### **Deterministic Per-Folder Indexes**
- **Spec-shaped listing** — `# <type>` groups of `* [Title](doc.md) - description`; only the bundle-root index carries frontmatter, and only `okf_version`
- **Canonical round-trip check** — an index that is not byte-identical to its own re-render is reported as non-canonical or tampered
- **Sidecar digest** — a `.index.digest` file commits the index's SHA-256 and every listed document's SHA-256; shallow verification is O(1), deep verification O(folder)
- **Nothing silently vanishes** — classified documents are listed with their label so a reader can gate on it
- **Path guards** — absolute paths, `..` traversal, sub-paths, non-`.md` suffixes, and reserved names are refused
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

### Folder Indexes (`arcokf.index`)

Every folder may carry a reserved `index.md`: a grouped listing an agent reads to see what a folder
holds before opening any file. `arcokf` only renders, parses and validates; the owning store decides
when to write one.

| Symbol | What It Does |
|---|---|
| `IndexEntry` | One line: `path`, `title`, `description`, `group` (document `type`), `classification` label |
| `render_folder_index(entries, root=…)` | Render the canonical index: folders first, then one `#` group per type |
| `validate_folder_index(folder, root=…, deep=…)` | Verify ONE folder against its sidecar (shallow O(1), deep O(folder)) |
| `folder_entry(path)` | Build the entry for one document (every valid document is listed, with its label) |

**Why the round-trip check:** the index re-renders the entries it just parsed and compares the result
to the file byte for byte. Anything a hand edit could do — reordering, an added link line, a tweaked
summary, a swapped digest — changes those bytes. That detects an edit of the index alone. A forged
index plus a recomputed sidecar still agrees with itself, so an owner that needs authenticity signs
the sidecar hash (`FolderIndexValidation.sidecar_sha`); `arcmemory` does this with the agent key.
Every read is one `O_NOFOLLOW` open checked with `fstat` (`read_regular_file`), and validation returns
the exact `text` and `digest` it verified so no caller reads the file a second time.

**Why every document is listed with its label:** silently omitting classified documents left
cleared agents with an empty index. An entry now carries `(classification: <label>)` and the reader
gates on it, so nothing vanishes and nothing is shown below its clearance.

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
