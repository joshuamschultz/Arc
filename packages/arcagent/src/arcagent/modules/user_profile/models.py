"""Pydantic models for per-user profile storage.

Three public models:
  ACL          — access control record embedded in YAML frontmatter
  DurableFact  — single append-only fact with provenance metadata
  UserProfile  — complete profile parsed from a markdown file

YAML frontmatter is delimited by ``---`` fences.  Everything after the
closing ``---`` is the markdown body that the LLM sees when the profile
is loaded into context.

Schema version:
  v1 — initial schema per SDD §3.6 (2026-04-18)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import yaml  # type: ignore[import-untyped]  # types-PyYAML not a hard dep
from pydantic import BaseModel, Field, field_validator

# Regex to extract YAML frontmatter between --- fences (non-greedy).
# The first fence must be at column 0; the second fence terminates the block.
_FRONTMATTER_RE = re.compile(
    r"^---\r?\n(.*?)^---\r?\n(.*)",
    re.DOTALL | re.MULTILINE,
)

# Regex to match a single durable-fact line including provenance comment.
# Format:  - Fact text  <!-- session_id=xxx ts=ISO -->
_DURABLE_FACT_RE = re.compile(
    r"^- (.+?)\s*<!--\s*session_id=(\S+)\s+ts=(\S+)\s*-->$"
)


class ACL(BaseModel):
    """Access control record for a user profile.

    Federal default: cross_user_shareable=False, agent_read=True.
    """

    owner: str = Field(description="User DID of the profile owner")
    agent_read: bool = Field(
        default=True,
        description="Whether the agent may read this profile",
    )
    cross_user_shareable: bool = Field(
        default=False,
        description="Whether this profile may be shared with other users",
    )

    model_config = {"frozen": True, "extra": "forbid"}


class DurableFact(BaseModel):
    """A single immutable fact appended to a user profile.

    Durable facts are append-only: once written they are never removed
    except by a GDPR tombstone.  Each fact carries its provenance so
    the tombstone can redact the session JSONL fields that produced it.

    Attributes:
        content:          Human-readable fact text.
        source_session_id: ID of the session that produced this fact.
        ts:               UTC timestamp when the fact was recorded.
    """

    content: str
    source_session_id: str
    ts: datetime

    @field_validator("ts", mode="before")
    @classmethod
    def parse_ts(cls, v: Any) -> datetime:
        """Accept ISO strings as well as datetime objects."""
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v))

    def to_markdown_line(self) -> str:
        """Serialise to the append-only markdown list format.

        Example output:
            - Alice prefers bullet summaries  <!-- session_id=abc ts=2026-04-18T12:00:00+00:00 -->
        """
        ts_str = self.ts.isoformat()
        return f"- {self.content}  <!-- session_id={self.source_session_id} ts={ts_str} -->"

    model_config = {"frozen": True}


class UserProfile(BaseModel):
    """Complete per-user profile loaded from ``user_profile/{user_did}.md``.

    The profile file is a markdown document with a YAML frontmatter block.
    The body is parsed into sections (Identity, Preferences, Durable Facts,
    Derived) but stored as raw strings so round-trips preserve whitespace.

    Construction:
        Use :meth:`from_markdown` to parse an existing file.
        Use :meth:`to_markdown` to serialise back to disk format.
    """

    user_did: str
    created: datetime
    classification: str = Field(
        default="unclassified",
        description="unclassified | cui | secret",
    )
    acl: ACL
    schema_version: int = 1

    # Markdown body sections — raw strings for round-trip fidelity
    identity_section: str = ""
    preferences_section: str = ""
    durable_facts: list[DurableFact] = Field(default_factory=list)
    derived_section: str = ""

    @field_validator("created", mode="before")
    @classmethod
    def parse_created(cls, v: Any) -> datetime:
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v))

    @field_validator("classification")
    @classmethod
    def validate_classification(cls, v: str) -> str:
        allowed = {"unclassified", "cui", "secret"}
        if v not in allowed:
            raise ValueError(f"classification must be one of {allowed}")
        return v

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_markdown(cls, text: str) -> UserProfile:
        """Parse a profile from its on-disk markdown representation.

        Raises:
            ValueError: if the file has no valid YAML frontmatter.
        """
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError("Profile has no YAML frontmatter delimited by ---")

        frontmatter_raw, body = m.group(1), m.group(2)
        fm: dict[str, Any] = yaml.safe_load(frontmatter_raw) or {}

        acl_raw = fm.get("acl", {})
        acl = ACL(
            owner=acl_raw.get("owner", fm.get("user_did", "")),
            agent_read=acl_raw.get("agent_read", True),
            cross_user_shareable=acl_raw.get("cross_user_shareable", False),
        )

        identity, preferences, durable_raw, derived = _parse_body_sections(body)

        durable_facts = _parse_durable_facts(durable_raw)

        return cls(
            user_did=fm["user_did"],
            created=fm["created"],
            classification=fm.get("classification", "unclassified"),
            acl=acl,
            schema_version=fm.get("schema_version", 1),
            identity_section=identity,
            preferences_section=preferences,
            durable_facts=durable_facts,
            derived_section=derived,
        )

    def to_markdown(self) -> str:
        """Serialise the profile back to its on-disk markdown form."""
        fm: dict[str, Any] = {
            "user_did": self.user_did,
            "created": self.created.isoformat(),
            "classification": self.classification,
            "acl": {
                "owner": self.acl.owner,
                "agent_read": self.acl.agent_read,
                "cross_user_shareable": self.acl.cross_user_shareable,
            },
            "schema_version": self.schema_version,
        }
        frontmatter = yaml.dump(fm, default_flow_style=False, allow_unicode=True)

        # Build body sections
        parts: list[str] = []

        parts.append("## Identity")
        if self.identity_section.strip():
            parts.append(self.identity_section.strip())
        parts.append("")

        parts.append("## Preferences")
        if self.preferences_section.strip():
            parts.append(self.preferences_section.strip())
        parts.append("")

        parts.append("## Durable Facts")
        for fact in self.durable_facts:
            parts.append(fact.to_markdown_line())
        parts.append("")

        parts.append("## Derived (dialectic)")
        if self.derived_section.strip():
            parts.append(self.derived_section.strip())
        parts.append("")

        body = "\n".join(parts)
        return f"---\n{frontmatter}---\n{body}"

    def body_bytes(self) -> int:
        """Return byte count of the markdown body (everything after frontmatter)."""
        body = self.to_markdown().split("---\n", 2)
        if len(body) < 3:
            return 0
        return len(body[2].encode("utf-8"))

    model_config = {"frozen": False}


# ---------------------------------------------------------------------------
# Private parsing helpers
# ---------------------------------------------------------------------------


def _parse_body_sections(body: str) -> tuple[str, str, str, str]:
    """Split body into (identity, preferences, durable_raw, derived) sections.

    Returns empty strings for missing sections.  The durable_raw section
    is the raw markdown content so that individual facts can be parsed.
    """
    sections: dict[str, str] = {
        "identity": "",
        "preferences": "",
        "durable facts": "",
        "derived (dialectic)": "",
    }
    current: str | None = None
    buf: list[str] = []

    for line in body.splitlines():
        header = line.strip().lower()
        if header.startswith("## "):
            # Save previous section
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            key = header[3:].strip()
            if key in sections:
                current = key
                buf = []
            else:
                # Unknown section — treat as continuation of previous
                buf.append(line)
        else:
            buf.append(line)

    if current is not None:
        sections[current] = "\n".join(buf).strip()

    return (
        sections["identity"],
        sections["preferences"],
        sections["durable facts"],
        sections["derived (dialectic)"],
    )


def _parse_durable_facts(raw: str) -> list[DurableFact]:
    """Parse the Durable Facts section into DurableFact objects.

    Lines not matching the expected format are silently skipped; this
    preserves forward-compatibility with manually edited files.
    """
    facts: list[DurableFact] = []
    for line in raw.splitlines():
        line = line.strip()
        m = _DURABLE_FACT_RE.match(line)
        if m:
            facts.append(
                DurableFact(
                    content=m.group(1),
                    source_session_id=m.group(2),
                    ts=datetime.fromisoformat(str(m.group(3))),
                )
            )
    return facts
