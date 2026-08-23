"""SPEC-047 — the first-class extension-point framework.

``arcagent/extension/`` (sibling of ``core/``, like ``brain/`` and ``skilladapt/``) holds
the generalized select-one mechanism plus the family registry + inspection for the CLI:

* :class:`ExtensionPoint` + :func:`select_extension` — the one copy of the choice
  dispatch, fail-closed BYO allowlist gate, and dotted-path importer (SPEC-041/044 dedup).
* ``families`` — the four extension-point families (brain, skills, tools, hook-builds).
* ``inspect`` — a pure read of what is selected / available / signed for ``arc ext inspect``.

The mechanism speaks only structural Protocols + primitives; it never names a concrete
implementation type or statically imports a builtin/BYO package.

SPEC-062 adds the connector seam alongside it:

* :class:`ExtensionAttachment` + its value types — the one hook contract any external
  system is reached through; core depends on it and on nothing more concrete.
* :class:`ExtensionManifest` + :func:`load_manifest` — the only document Arc parses
  from an extension bundle.
"""

from __future__ import annotations

from arcagent.extension.attachment import (
    Classification,
    ExtensionAttachment,
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.manifest import ExtensionManifest, load_manifest
from arcagent.extension.point import ExtensionPoint
from arcagent.extension.select import select_extension
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceAdapter,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)

__all__ = [
    "Classification",
    "ExtensionAttachment",
    "ExtensionManifest",
    "ExtensionPoint",
    "FetchSourceObject",
    "InspectSource",
    "ProbeResult",
    "Requirement",
    "RequirementKind",
    "SourceAdapter",
    "SourceContent",
    "SourceDataShape",
    "SourceDescription",
    "SourceError",
    "SourceFailureCode",
    "SourceObject",
    "SourceObjectKind",
    "SyncSource",
    "SyncSourcePage",
    "ToolOutcome",
    "ToolResult",
    "ToolSpec",
    "load_manifest",
    "select_extension",
]
