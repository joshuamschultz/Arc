"""Starter workflow templates shipped inside the package (J3 F6, G5).

A blank graph is the wrong starting point for most operators. Four complete,
valid bundles cover the common shapes: an intake with a human decision, a
fan-out that is synthesized into one answer, a maker whose work a checker and a
human review, and a scheduled watcher that speaks only when something changed.

A template is data, not trust. Instantiating one produces an ordinary
unsigned draft through the same write path every author uses; the operator
signs it afterwards. Nothing here carries a signature, because a signature is
pinned to one deployment's operator key and could never be shipped in a package.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

from arcteam.workflow.errors import InvalidWorkflowIdError, WorkflowError
from arcteam.workflow.models import WORKFLOW_ID_PATTERN, parse_definition
from arcteam.workflow.serialize import referenced_files

_DEFINITION = "workflow.toml"
_LEGAL_ID = re.compile(WORKFLOW_ID_PATTERN)


class TemplateNotFoundError(WorkflowError):
    """The named template is not one this build ships."""


@dataclass(frozen=True)
class TemplateInfo:
    """What a template picker shows: a stable id, a title, and one sentence."""

    id: str
    title: str
    description: str


def _root() -> Traversable:
    return files("arcteam.workflow").joinpath("templates")


def _template_dirs() -> list[Traversable]:
    return sorted(
        (entry for entry in _root().iterdir() if entry.joinpath(_DEFINITION).is_file()),
        key=lambda entry: entry.name,
    )


def list_templates() -> tuple[TemplateInfo, ...]:
    """Every shipped template, by id."""
    infos: list[TemplateInfo] = []
    for directory in _template_dirs():
        workflow = _read_document(directory)["workflow"]
        infos.append(
            TemplateInfo(
                id=directory.name,
                title=str(workflow.get("name") or directory.name),
                description=str(workflow.get("description") or ""),
            )
        )
    return tuple(infos)


def load_template(
    template_id: str, *, workflow_id: str, owner: str | None = None
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """A template as ``(document, companion files)`` ready for the control plane.

    The id is matched against the shipped list, never joined onto a path, so a
    caller-supplied name cannot reach outside the templates directory. The
    workflow id is the new bundle's own and is checked by the same rule the
    store enforces, so a refusal happens here with a clear reason rather than
    deep inside a write.

    Raises:
        TemplateNotFoundError: no shipped template has that id.
        InvalidWorkflowIdError: ``workflow_id`` is not a legal bare name.
    """
    if not _LEGAL_ID.fullmatch(workflow_id):
        raise InvalidWorkflowIdError(
            f"{workflow_id!r} is not a workflow id; use a bare name matching {WORKFLOW_ID_PATTERN}"
        )
    directory = next((d for d in _template_dirs() if d.name == template_id), None)
    if directory is None:
        known = ", ".join(info.id for info in list_templates())
        raise TemplateNotFoundError(f"no template {template_id!r}; available: {known}")
    document = _read_document(directory)
    document["workflow"]["id"] = workflow_id
    if owner is not None:
        document["workflow"]["owner"] = owner
    bodies = {
        reference: directory.joinpath(reference).read_bytes()
        for reference in referenced_files(parse_definition(document))
    }
    return document, bodies


def _read_document(directory: Traversable) -> dict[str, Any]:
    return tomllib.loads(directory.joinpath(_DEFINITION).read_text(encoding="utf-8"))


__all__ = [
    "TemplateInfo",
    "TemplateNotFoundError",
    "list_templates",
    "load_template",
]
