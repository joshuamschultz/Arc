# arcokf - The Typed Document Contract for Knowledge

> **Building with Arc**  ·  Build  ·  Packages
> **For** Engineers writing code against Arc
> [Docs home](../../README.md)  ·  [Package index](../package-index.md)  ·  [arcmemory →](arcmemory.md)

---

## In one breath

`arcokf` is Arc's dependency-light **Open Knowledge Format (OKF) v0.2**
contract — the typed boundary that turns a folder of Markdown into a corpus a
store can trust. It parses a knowledge document (Markdown with a YAML
frontmatter carrying a non-empty string `type`), rejects malformed YAML and
unsafe links, and returns **structured diagnostics** so a fail-closed store can
report every problem before it writes an invalid artifact.

It is a **near-leaf contract package**. Its only runtime dependency is PyYAML;
it imports **no Arc package at all**, not even `arctrust`. Dependencies point one
way — a consumer such as `arcmemory` imports `arcokf`, and `arcokf` never imports
a consumer — which is what lets an importer, a workspace, or a third-party
plugin validate a knowledge document with no agent, memory engine, or crypto
stack installed.

```mermaid
flowchart TB
    classDef consumer fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef contract fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef dep fill:#E9EAEB,stroke:#7F7F7F,color:#0B1220

    arcmemory[arcmemory]:::consumer --> arcokf
    arcteam[arcteam]:::consumer --> arcokf
    arccli[arccli]:::consumer --> arcokf
    arcagent[arcagent]:::consumer --> arcokf
    arcokf["arcokf<br/>OKF v0.2 · parse · validate · render · index"]:::contract
    arcokf --> pyyaml[PyYAML<br/>the only runtime dependency]:::dep
```

---

## Why a separate contract package

`arcokf` is deliberately **not** part of `arcmemory`. OKF is the canonical
document boundary shared by workspaces, shared knowledge, importers, and
third-party plugins — and every one of those consumers must be able to validate
a document *without* installing a memory engine. Keeping the contract in a leaf
package means either side of the seam — the format, or the store that uses it —
can be removed or rewritten behind the same `parse` / `render` / `validate`
functions without breaking the other.

The one required field is what does the work. A knowledge document is Markdown
with YAML frontmatter carrying a **non-empty string `type`**. That single field
is the difference between a typed corpus and a pile of notes: a store can refuse
to write anything that would not parse back.

---

## The v0.2 document shape

```markdown
---
type: Entity
title: Arc
tags: [agent, platform]
---
# Arc

Arc is an accountable agent framework.
```

Reserved workspace files — `context.md`, `index.md`, `log.md` — may omit
frontmatter, and are **refused if they try to become typed concept documents**
(they carry operational state, not knowledge). Operational TOML, JSON, JSONL,
signature, and audit files are not OKF documents and sit intentionally outside
this contract.

The bounds are constants in `arcokf.core`, and every one exists to stop an
input from becoming a denial-of-service:

| Constant | Value | Why |
|---|---|---|
| `MAX_DOCUMENT_BYTES` | 2,000,000 | A knowledge document is a document, not a data dump |
| `MAX_FRONTMATTER_BYTES` | 256,000 | Metadata is metadata; the body carries the content |
| `MAX_NESTING` | 32 | Bounds the recursive structure walk |
| `MAX_ALIASES` | 16 | Stops YAML alias expansion (the "billion laughs" class) |
| `VERSION` | `"0.2"` | The contract version this build implements |

---

## Documents and validation (`arcokf.core`)

The core module parses and validates one document at a time. The design choice
that shapes the whole API: **validation returns diagnostics, it does not raise**
— because a store about to write a batch of documents needs to know *everything*
wrong with them before it writes any. An exception reports the first problem and
destroys the rest of the pass; a `ValidationResult` reports all of them and
leaves the write to the caller.

| Symbol | What it does |
|---|---|
| `Document` | Frozen dataclass: `metadata` mapping, Markdown `body`, optional source `path` |
| `validate(source, *, path=None)` | Validate `str` or `bytes`. **Never raises** — returns a `ValidationResult` |
| `parse(source, *, path=None)` | Validate, then return the `Document`; raises `OKFValidationError` if invalid |
| `render(document)` | Serialize to `---` frontmatter (sorted keys, unicode preserved) + body, **re-validating the result before returning it** so `render` never hands back a bad artifact |
| `lint(path)` | Validate one UTF-8 file from disk, keeping its POSIX path on every diagnostic. An unreadable file is a diagnostic, not an exception |
| `ValidationResult` | `valid` flag, `diagnostics` tuple, and the `Document` — present only when the result is clean |
| `Diagnostic` | Frozen `code` / `message` / `path` triple |
| `DiagnosticCode` | The `StrEnum` of every refusal reason |
| `OKFValidationError` | Raised by `parse` / `render`; carries the full `diagnostics` tuple |

The two entry points — `validate` (never raises) and `parse` (raises on
invalid) — exist for two call sites. Use `parse` where invalid input must stop
the caller cold; use `validate` or `lint` where a caller needs to collect and
report structured diagnostics first.

### The diagnostic codes

The eleven members of `DiagnosticCode` are machine-routable by the calling
store — each names exactly one refusal reason:

`invalid_utf8`, `invalid_frontmatter`, `duplicate_key`, `missing_type`,
`invalid_type`, `document_too_large`, `frontmatter_too_large`,
`nesting_too_deep`, `too_many_aliases`, `wiki_link`, `reserved_document`.

### The hardened YAML loader

Frontmatter is parsed by a `SafeLoader` subclass built in `core.py`, not by a
default loader. Three hardening properties come from it:

- **No arbitrary object construction, ever** — it is a `SafeLoader`, so a
  frontmatter tag cannot instantiate a Python object.
- **Duplicate-key refusal** — a repeated frontmatter key is a `duplicate_key`
  diagnostic, not a silent last-write-wins overwrite (the loader's mapping
  constructor rejects it).
- **Alias-bomb ceiling** — more than `MAX_ALIASES` (16) aliases stops
  composition, defeating the billion-laughs expansion attack (`_compose_node`).

A body containing a wiki-link (`[[target]]`) fails validation with a `wiki_link`
diagnostic: `[[…]]` is not a standard OKF link, so shipping one would ship a
dangling reference rather than a real link.

---

## Folder indexes (`arcokf.index`)

Every folder of a bundle may carry its own reserved `index.md`: a grouped listing
(`# <type>` headings, `* [Title](doc.md) - description` lines, child folders as
`* [name/](name/index.md) - N docs`). Only the bundle-root index may carry
frontmatter, and only `okf_version: "0.2"`. Integrity data lives beside the
listing in a `.index.digest` sidecar, so the visible lines stay spec-shaped.
`arcokf` only renders, parses and validates; the **owning store decides when to
write one**.

| Symbol | What it does |
|---|---|
| `IndexEntry` | One line: `path`, `title`, `description`, `group` (the document `type`), `classification` label; plus sidecar-only `digest` / `count` |
| `render_folder_index(entries, root=…)` | Render the canonical index: folders first, then one `#` group per type, sorted, deterministic |
| `parse_folder_index(text, root=…)` | Parse index text back to entries; rejects anything not byte-identical to its re-render |
| `render_folder_digest` / `read_folder_digest` | The sidecar: the index's own SHA-256, each document's SHA-256, child-folder counts |
| `validate_folder_index(folder, root=…, deep=…)` | Verify ONE folder. Shallow is O(1): canonical and matching its sidecar. Deep adds O(folder): every listed document still exists with its digest and its listed title/description, and no valid document is unlisted |
| `folder_entry(path)` | Build the entry for one document (every valid document is listed, with its classification label) |

Properties:

- **Canonical round-trip check.** Parsing re-renders and compares byte for byte, so
  any hand edit (reorder, added line, tweaked summary) is detected without a signature.
- **Sidecar digest.** An index edited without its sidecar fails shallow validation.
  The sidecar is an unkeyed hash, so on its own it proves only that the two files
  agree: anyone who can write `index.md` can recompute it. Authenticity is the
  owner's job. `arcmemory` signs every sidecar hash with the agent's key in a
  `.okf.seal` per collection and checks `FolderIndexValidation.sidecar_sha` against
  it; the deep check catches a forged pair for a caller with no seal.
- **Verify once, use those bytes.** `validate_folder_index` reads the index and the
  sidecar once each and returns the exact `text` and `digest` it checked; callers
  use them and never re-open the file. Every read goes through
  `read_regular_file` (`O_NOFOLLOW` + `fstat`: a regular file, never a symlink leaf,
  optionally the same inode the caller listed), and `folder_entry` hashes and parses
  one buffer.
- **Nothing silently vanishes.** Classified documents are listed with
  `(classification: <label>)`; the reader gates on the label.
- **Path guards.** Absolute paths, `..`, sub-paths, non-`.md` suffixes and reserved
  names are refused when an entry is rendered or parsed; operational and hidden
  directories are never listed.
- **Root frontmatter.** `validate(..., bundle_root=True)` accepts `index.md`
  frontmatter only when it is exactly `{okf_version: "0.2"}`; everywhere else an
  index with frontmatter is rejected. `title`, `description` and `generated` are
  optional on concept documents but type-checked when present.

---

## Worked example

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

Building and verifying one folder's index:

```python
from pathlib import Path

from arcokf import (
    DIGEST_NAME,
    folder_entry,
    render_folder_digest,
    render_folder_index,
    validate_folder_index,
)

folder = Path("workspace/knowledge")
entries = [e for p in sorted(folder.glob("*.md")) if p.name != "index.md" and (e := folder_entry(p))]
text = render_folder_index(entries, root=False)
(folder / "index.md").write_text(text, encoding="utf-8")
(folder / DIGEST_NAME).write_text(render_folder_digest(text, tuple(entries)), encoding="utf-8")

# Later: shallow = canonical + matches its sidecar; deep = every listed document still matches.
assert validate_folder_index(folder).valid
assert validate_folder_index(folder, deep=True).valid
```

---

## How Arc consumes it

`arcmemory` is the primary consumer, and it reaches `arcokf` through a single
choke point rather than scattering parse calls across the engine:

- **`arcmemory.mdfile`** is *"the single place that reads, renders, and"* writes
  a frontmatter document. It imports `Document`, `OKFValidationError`, `parse`,
  and `render` from `arcokf`, so every memory file is a validated OKF v0.2
  document on the way in and out.
- **`arcmemory.collection_index`** (`OkfIndexMaintainer`) keeps every folder's
  `index.md` current: stores only mark a folder dirty (O(1)), one debounced
  background task regenerates dirty folders off the event loop reading only
  changed documents, and a parent is touched only when a child's document count
  moves. `arcmemory.index.okf_walk.OkfWalker` retrieves by walking root index,
  folder index, documents.
- **`arcteam`** (shared knowledge and per-agent memory storage), **`arccli`**
  (agent create / run), and several **`arcagent` modules** (workpad,
  user_profile) validate documents through the same contract.

Because the contract is a leaf, any of these can be rewritten behind the
unchanged `parse` / `render` / `validate` functions without touching `arcokf`.

---

## Threat surface

OKF is where untrusted knowledge enters the agent, so its refusals map directly
onto the OWASP LLM and agentic surfaces:

| Threat | How `arcokf` defends |
|---|---|
| **LLM04 Data poisoning** | A poisoned or malformed document fails validation and is never written; a store can refuse an entire batch on the first diagnostic without a partial commit |
| **ASI06 Memory / context poisoning** | The canonical round-trip check and per-document digests make an edited corpus detectable without re-reading it; a hand-tampered `index.md` fails canonicality |
| **Classification laundering** | The classification floor lets only `unclassified` documents into a shared index; a missing or elevated label is omitted, never listed |
| **Path traversal / arbitrary read** | Entry construction refuses absolute paths, `..` segments, reserved names, and operational directories before an entry exists |
| **Denial of service** | Hard bounds on document size, frontmatter size, nesting depth, and YAML aliases stop oversized inputs and alias bombs at parse time |
| **Unsafe links** | A `[[wiki-link]]` in a body is a `wiki_link` refusal — a non-standard link never ships as a dangling reference |

---

## Failure modes

- **Invalid input to `parse` / `render`** → `OKFValidationError`, carrying the
  full `diagnostics` tuple. Every problem is on the exception, not just the first.
- **Invalid input to `validate` / `lint`** → a `ValidationResult` with
  `valid=False` and a populated `diagnostics` tuple; **no exception**, and
  `result.document` is `None`.
- **Unreadable file passed to `lint`** → an `invalid_utf8` diagnostic on the
  result (the `OSError` is captured, not raised).
- **Tampered or stale folder index** → `validate_folder_index` returns
  `FolderIndexValidation(valid=False, error=…)`; the underlying
  `FolderIndexError` is surfaced as `error` text, never raised to the caller.

Every failure mode leaves the decision — and the write — with the caller. That
is the point of the contract.

---

## Verified public surface

> Introspected from `arcokf/__init__.py` on the current commit. Every name is
> importable as `from arcokf import <name>`.

**From `arcokf.core`:** `VERSION`, `Document`, `Diagnostic`, `DiagnosticCode`,
`ValidationResult`, `OKFValidationError`, `validate`, `parse`, `render`, `lint`.

**From `arcokf.index`:** `IndexEntry`, `FolderDigest`, `FolderIndexValidation`,
`FolderIndexError`, `render_folder_index`, `parse_folder_index`,
`render_folder_digest`, `parse_folder_digest`, `read_folder_digest`,
`validate_folder_index`, `folder_entry`, `folder_summary_entry`, `listable_dir`,
`listable_file`, and the constants `INDEX_NAME`, `DIGEST_NAME`, `FOLDERS_GROUP`.

---

## Status

- **Contract version:** OKF v0.2 (`VERSION == "0.2"`)
- **Runtime dependency:** PyYAML only — no Arc imports
- **Tests:** `packages/arcokf/tests/` (`test_okf.py`, `test_index_v02.py`)
- **Type check:** `mypy --strict` clean · **Lint:** `ruff check` clean

```bash
uv run --no-sync pytest packages/arcokf/tests
```

---

## Next Steps

- [arcmemory](arcmemory.md) — the primary consumer; the dual-speed memory engine
  that stores every file as a validated OKF document
- [Package index](../package-index.md) — all Arc packages
- [The Seam Model](../../concepts/seam-model.md) — why the document format is a
  leaf contract, not a feature of the memory engine
